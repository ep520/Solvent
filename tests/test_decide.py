import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from callguard import decide as dec  # noqa: E402
from callguard import extract as ex  # noqa: E402
from callguard import pipeline  # noqa: E402

POLICIES = ex.load_json(ROOT / "config" / "policies.json")
KEYWORDS = ex.load_json(ROOT / "data" / "Stichwortliste.json")
SEGMENTS = [
    {"id": "s1", "speaker": None, "start": 0.0, "end": 4.0, "text": "Mini Schwöschter het gseit, d Quartalszahle sind no nöd publiziert."},
    {"id": "s2", "speaker": None, "start": 4.0, "end": 8.0, "text": "Grad drum möcht ich d Aktie jetzt chaufe,", "avg_logprob": -0.1},
    {"id": "s3", "speaker": None, "start": 8.0, "end": 12.0, "text": "vor de Meldig. Das isch es abgloffnigs Muster: 482913.", "avg_logprob": -1.2},
    {"id": "s4", "speaker": None, "start": 12.0, "end": 16.0, "text": "Ich weiss nöd, öb ich das dörf säge.", "avg_logprob": -0.1},
]


def cond(state, *refs):
    return {"state": state, "evidence": [{"segment_id": s, "quote": q} for s, q in refs]}


def trade(**overrides):
    conds = {
        "own_trade_request": cond("true", ("s2", "möcht ich d Aktie jetzt chaufe")),
        "information_nonpublic": cond("true", ("s1", "no nöd publiziert")),
        "information_market_relevant": cond("true", ("s1", "Quartalszahle")),
        "request_based_on_information": cond("true", ("s2", "Grad drum")),
    }
    return ("trade", {"conditions": {**conds, **overrides}})


def extraction(*events, drop=()):
    fams = {name: {"events": []} for name in POLICIES["families"] if name not in drop}
    for family, event in events:
        fams[family]["events"].append(event)
    return {"families": fams}


def run(*events, threshold=None, segments=SEGMENTS, drop=()):
    return dec.decide(extraction(*events, drop=drop), segments, POLICIES, threshold=threshold)


class DecideTest(unittest.TestCase):
    def test_all_conditions_true_is_alarm(self):
        d = run(trade())
        self.assertEqual((d["label"], d["reasons"]), ("alarm", []))
        self.assertEqual(d["events"][0]["summary"], "All conditions established.")

    def test_grounded_false_is_no_alert(self):
        d = run(trade(own_trade_request=cond("false", ("s4", "Ich weiss nöd"))))
        self.assertEqual((d["label"], d["events"][0]["status"]), ("no_alert", "absent"))

    def test_false_without_evidence_becomes_unknown_and_technical_review(self):
        d = run(trade(own_trade_request=cond("false")))
        self.assertEqual((d["label"], d["reasons"]), ("review", ["technical_uncertainty"]))
        self.assertTrue(d["events"][0]["conditions"]["own_trade_request"]["downgraded"])

    def test_unknown_condition_is_review_missing_fact_with_open_question(self):
        conds = {"value_spoken": cond("true", ("s3", "482913")), "access_secret": cond("true", ("s3", "482913")),
                 "secret_active": cond("unknown")}
        d = run(("access", {"conditions": conds}))
        self.assertEqual((d["label"], d["reasons"]), ("review", ["missing_policy_fact"]))
        self.assertIn("still valid", d["events"][0]["open_questions"][0])

    def test_quote_not_found_downgrades_to_technical_review(self):
        d = run(trade(information_nonpublic=cond("true", ("s1", "the figures are secret"))))
        self.assertEqual((d["label"], d["reasons"]), ("review", ["technical_uncertainty"]))

    def test_quote_matching_ignores_case_and_punctuation(self):
        self.assertEqual(run(trade(information_nonpublic=cond("true", ("s1", "Quartalszahle sind no NÖD publiziert"))))["label"], "alarm")

    def test_quote_spanning_two_segments_records_both(self):
        d = run(trade(request_based_on_information=cond("true", ("s2", "jetzt chaufe, vor de Meldig"))))
        ref = d["events"][0]["conditions"]["request_based_on_information"]["evidence"][0]
        self.assertEqual((ref["segment_ids"], ref["start"], ref["end"]), (["s2", "s3"], 4.0, 12.0))

    def test_quote_in_the_neighbour_segment_is_found_but_not_further_away(self):
        near = run(trade(information_nonpublic=cond("true", ("s2", "no nöd publiziert"))))
        far = run(trade(information_nonpublic=cond("true", ("s3", "no nöd publiziert"))))
        self.assertEqual(near["events"][0]["conditions"]["information_nonpublic"]["evidence"][0]["segment_ids"], ["s1", "s2"])
        self.assertEqual(far["label"], "review")

    def test_small_copy_slip_is_aligned_and_shows_the_real_words(self):
        d = run(trade(information_nonpublic=cond("true", ("s1", "d Quartalszahlen sind no nöd publiziert"))))
        ref = d["events"][0]["conditions"]["information_nonpublic"]["evidence"][0]
        self.assertEqual(d["label"], "alarm")
        self.assertEqual(ref["quote"], "d Quartalszahle sind no nöd publiziert")
        self.assertLess(ref["match"], 1.0)

    def test_segment_id_without_leading_zeros_is_resolved(self):
        segs = [{**s, "id": f"s{i:03d}"} for i, s in enumerate(SEGMENTS, 1)]
        d = run(trade(own_trade_request=cond("true", ("s2", "möcht ich d Aktie jetzt chaufe")),
                      information_nonpublic=cond("true", ("s01", "no nöd publiziert")),
                      information_market_relevant=cond("true", ("s1", "Quartalszahle")),
                      request_based_on_information=cond("true", ("s002", "Grad drum"))), segments=segs)
        self.assertEqual(d["label"], "alarm")
        self.assertEqual(d["events"][0]["conditions"]["own_trade_request"]["evidence"][0]["segment_ids"], ["s002"])

    def test_flipped_negation_is_never_aligned(self):
        d = run(trade(information_nonpublic=cond("true", ("s1", "d Quartalszahle sind scho publiziert"))))
        self.assertEqual((d["label"], d["reasons"]), ("review", ["technical_uncertainty"]))

    def test_low_similarity_is_not_aligned(self):
        d = run(trade(information_nonpublic=cond("true", ("s1", "d Zahle sind vertraulich"))))
        self.assertEqual(d["label"], "review")

    def test_low_asr_quality_on_a_decisive_condition_is_review_below_threshold(self):
        conds = {n: cond("true", ("s3", "482913")) for n in ("value_spoken", "access_secret", "secret_active")}
        event = ("access", {"conditions": conds})
        d = run(event)
        self.assertEqual(d["reasons"], ["below_escalation_threshold"])
        self.assertEqual(d["events"][0]["low_quality"], ["value_spoken", "access_secret", "secret_active"])
        self.assertEqual(run(event, threshold=0.2)["label"], "alarm")

    def test_one_good_quote_per_condition_is_enough(self):
        conds = {n: cond("true", ("s3", "482913"), ("s4", "Ich weiss nöd")) for n in ("value_spoken", "access_secret", "secret_active")}
        self.assertEqual(run(("access", {"conditions": conds}))["label"], "alarm")

    def test_missing_quality_is_reported_not_replaced_by_a_perfect_score(self):
        d = run(trade(), threshold=0.9)
        self.assertEqual(d["label"], "alarm")
        self.assertEqual(d["events"][0]["quality_unavailable"],
                         ["information_nonpublic", "information_market_relevant"])

    def test_missing_family_is_technical_review(self):
        d = run(drop=("splitting",))
        self.assertEqual((d["label"], d["reasons"]), ("review", ["technical_uncertainty"]))
        self.assertIn("splitting", d["issues"][0])

    def test_empty_families_are_no_alert(self):
        self.assertEqual(run()["label"], "no_alert")

    def test_numbers_never_alarm(self):
        d = run(("numbers", {"object_type": "full_payment_card_number", "evidence": [{"segment_id": "s3", "quote": "482913"}]}))
        self.assertEqual((d["label"], d["events"][0]["status"]), ("no_alert", "not_applicable"))

    def test_alarm_wins_and_failed_extraction_is_technical_review(self):
        absent = ("access", {"conditions": {"value_spoken": cond("false", ("s4", "Ich weiss nöd")),
                                            "access_secret": cond("unknown"), "secret_active": cond("unknown")}})
        self.assertEqual(run(absent, trade())["label"], "alarm")
        self.assertEqual(dec.decide({}, SEGMENTS, POLICIES, error="timeout")["reasons"], ["technical_uncertainty"])

    def test_explanation_shows_rule_states_quotes_and_quality(self):
        text = dec.explain(run(trade()), SEGMENTS)
        self.assertIn("DECISION: ALARM", text)
        self.assertIn('s2 @ 4.0s q=0.905: "Grad drum"', text)
        self.assertIn("ASR quality not available", text)


class MissingOutcomeTest(unittest.TestCase):
    """Outcomes the dataset has no example of: trade→review, disclosure→review, documentation→no_alert."""

    def test_trade_with_unestablished_source_is_review(self):
        d = run(trade(information_nonpublic=cond("unknown")))
        self.assertEqual((d["label"], d["reasons"]), ("review", ["missing_policy_fact"]))

    def test_disclosure_with_unknown_authority_is_review(self):
        conds = {"advisor_disclosed": cond("true", ("s3", "482913")), "third_party_detail": cond("true", ("s3", "482913")),
                 "authority_absent": cond("unknown")}
        d = run(("disclosure", {"conditions": conds}))
        self.assertEqual((d["label"], d["reasons"]), ("review", ["missing_policy_fact"]))

    def test_documentation_omitting_an_irrelevant_fact_is_no_alert(self):
        conds = {"concealment_requested": cond("true", ("s4", "Ich weiss nöd")),
                 "fact_relevant": cond("false", ("s4", "öb ich das dörf säge")),
                 "designated_recipient": cond("true", ("s4", "Ich weiss nöd"))}
        self.assertEqual(run(("documentation", {"conditions": conds}))["label"], "no_alert")


class DataTest(unittest.TestCase):
    def test_keyword_matcher_reproduces_every_annotated_hit(self):
        for script in sorted((ROOT / "data" / "Skript_mit_Sollbewertung").glob("*.txt")):
            line = re.search(r"^Keywordstellen: (.+)$", script.read_text(encoding="utf-8"), re.M).group(1)
            expected = set(re.findall(r"(K\d\d) '([^']+)' (T\d{3})", line))
            segments = ex.segments_from_transcript(ROOT / "data" / "Transkript" / script.name)
            found = {(h["keyword"], h["phrase"], h["segment_id"]) for h in ex.keyword_hits(segments, KEYWORDS)}
            self.assertEqual(found, expected, script.name)

    def test_transcript_parsing_and_speaker_hiding(self):
        path = ROOT / "data" / "Transkript" / "Stufe1_D02.txt"
        segs = ex.segments_from_transcript(path)
        self.assertEqual((len(segs), segs[3]["id"], segs[3]["speaker"]), (14, "T004", "Kundin"))
        self.assertIsNone(ex.segments_from_transcript(path, speakers=False)[3]["speaker"])

    def test_gold_is_read_from_expected_assessment(self):
        self.assertEqual(pipeline.gold("Stufe3_C17"), {"label": "review", "family": "access", "event": "undetermined",
                                                       "evidence": ["T004", "T010"]})

    def test_extraction_is_cached_per_model_and_prompt(self):
        profile = {"name": "fake", "model": "m"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ex.models, "check", return_value=profile), \
                mock.patch.object(ex.models, "chat_json", return_value={"events": []}) as chat:
            first = ex.extract(SEGMENTS, POLICIES, cache_dir=tmp)
            second = ex.extract(SEGMENTS, POLICIES, cache_dir=tmp)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["_meta"]["model"], "m")


if __name__ == "__main__":
    unittest.main()
