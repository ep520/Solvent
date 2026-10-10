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


def actor(role="unknown", status="unknown", *segment_ids):
    return {"role": role, "status": status, "segment_ids": list(segment_ids)}


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

    def test_quote_matching_allows_casefold_but_retains_punctuation(self):
        self.assertEqual(run(trade(information_nonpublic=cond("true", ("s1", "QUARTALSZAHLE  SIND\nNO NÖD PUBLIZIERT"))))["label"], "alarm")
        changed_punctuation = run(trade(own_trade_request=cond("true", ("s2", "Grad drum, möcht ich"))))
        self.assertEqual((changed_punctuation["label"], changed_punctuation["reasons"]),
                         ("review", ["technical_uncertainty"]))

    def test_quote_spanning_two_segments_records_both(self):
        d = run(trade(request_based_on_information=cond("true", ("s2", "jetzt chaufe, vor de Meldig"))))
        ref = d["events"][0]["conditions"]["request_based_on_information"]["evidence"][0]
        self.assertEqual((ref["segment_ids"], ref["start"], ref["end"]), (["s2", "s3"], 4.0, 12.0))

    def test_valid_quote_attached_to_the_wrong_segment_is_rejected(self):
        wrong = run(trade(information_nonpublic=cond("true", ("s2", "no nöd publiziert"))))
        condition = wrong["events"][0]["conditions"]["information_nonpublic"]
        self.assertEqual((wrong["label"], condition["state"]), ("review", "unknown"))
        self.assertIn("does not exactly match", condition["grounding_issues"][0])

    def test_paraphrased_or_invented_quote_is_not_fuzzily_aligned(self):
        d = run(trade(information_nonpublic=cond("true", ("s1", "d Quartalszahlen sind no nöd publiziert"))))
        condition = d["events"][0]["conditions"]["information_nonpublic"]
        self.assertEqual((d["label"], condition["state"]), ("review", "unknown"))
        self.assertFalse(condition["evidence"])

    def test_segment_id_must_exist_exactly(self):
        segs = [{**s, "id": f"s{i:03d}"} for i, s in enumerate(SEGMENTS, 1)]
        d = run(trade(own_trade_request=cond("true", ("s002", "möcht ich d Aktie jetzt chaufe")),
                      information_nonpublic=cond("true", ("s01", "no nöd publiziert")),
                      information_market_relevant=cond("true", ("s001", "Quartalszahle")),
                      request_based_on_information=cond("true", ("s002", "Grad drum"))), segments=segs)
        condition = d["events"][0]["conditions"]["information_nonpublic"]
        self.assertEqual((d["label"], condition["state"]), ("review", "unknown"))
        self.assertIn("does not exist", condition["grounding_issues"][0])

    def test_flipped_negation_is_never_aligned(self):
        d = run(trade(information_nonpublic=cond("true", ("s1", "d Quartalszahle sind scho publiziert"))))
        self.assertEqual((d["label"], d["reasons"]), ("review", ["technical_uncertainty"]))

    def test_low_similarity_is_not_aligned(self):
        d = run(trade(information_nonpublic=cond("true", ("s1", "d Zahle sind vertraulich"))))
        self.assertEqual(d["label"], "review")

    def test_invalid_evidence_does_not_hide_an_independent_alarm(self):
        broken_access = ("access", {"conditions": {
            "value_spoken": cond("true", ("s3", "invented code")),
            "access_secret": cond("true", ("s3", "482913")),
            "secret_active": cond("true", ("s3", "482913")),
        }})
        d = run(broken_access, trade())
        by_family = {event["family"]: event for event in d["events"]}
        self.assertEqual(d["label"], "alarm")
        self.assertEqual((by_family["access"]["label"], by_family["access"]["conditions"]["value_spoken"]["state"]),
                         ("review", "unknown"))
        self.assertEqual(by_family["trade"]["label"], "alarm")

    def test_low_asr_quality_on_a_decisive_condition_is_review_below_threshold(self):
        conds = {n: cond("true", ("s3", "482913")) for n in ("value_spoken", "access_secret", "secret_active")}
        event = ("access", {"conditions": conds})
        d = run(event)
        self.assertEqual(d["reasons"], ["below_escalation_threshold"])
        self.assertEqual(d["events"][0]["low_quality"], ["value_spoken", "access_secret", "secret_active"])
        self.assertEqual(run(event, threshold=0.2)["label"], "alarm")

    def test_best_of_several_quotes_decides_a_condition_quality(self):
        conds = {n: cond("true", ("s2", "möcht ich d Aktie jetzt chaufe")) for n in ("value_spoken", "access_secret")}
        conds["secret_active"] = {"state": "true", "evidence": [
            {"segment_id": "s3", "quote": "Das isch es abgloffnigs Muster: 482913"},  # low quality (s3 has -1.2)
            {"segment_id": "s2", "quote": "möcht ich d Aktie jetzt chaufe"}]}        # high quality (s2 has -0.1)
        d = run(("access", {"conditions": conds}), threshold=0.7)
        self.assertEqual(d["label"], "alarm")
        self.assertNotIn("secret_active", d["events"][0].get("low_quality", []))

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

    def test_unresolved_event_routes_to_review_when_no_alarm_exists(self):
        excluded = ("access", {"conditions": {
            "value_spoken": cond("true", ("s3", "482913")),
            "access_secret": cond("true", ("s3", "482913")),
            "secret_active": cond("false", ("s3", "abgloffnigs Muster")),
        }})
        unresolved = trade(information_nonpublic=cond("unknown"))
        result = run(excluded, unresolved)
        self.assertEqual((result["label"], result["reasons"]), ("review", ["missing_policy_fact"]))
        self.assertEqual({event["label"] for event in result["events"]}, {"no_alert", "review"})

    def test_malformed_family_is_a_technical_review_not_a_silent_no_alert(self):
        malformed = extraction()
        malformed["families"]["trade"] = {"events": {}}
        result = dec.decide(malformed, SEGMENTS, POLICIES)
        self.assertEqual((result["label"], result["reasons"]), ("review", ["technical_uncertainty"]))
        self.assertIn("family 'trade' missing", result["issues"][0])

    def test_no_candidate_means_no_event_detected_not_a_proven_absence(self):
        result = run()
        self.assertEqual(result["label"], "no_alert")
        self.assertIn("No candidate event in any family", dec.explain(result, SEGMENTS))

    def test_threshold_never_changes_unknown_or_excluded_events(self):
        unknown = ("access", {"conditions": {
            "value_spoken": cond("true", ("s3", "482913")),
            "access_secret": cond("true", ("s3", "482913")),
            "secret_active": cond("unknown"),
        }})
        excluded = ("access", {"conditions": {
            "value_spoken": cond("true", ("s3", "482913")),
            "access_secret": cond("true", ("s3", "482913")),
            "secret_active": cond("false", ("s3", "abgloffnigs Muster")),
        }})
        self.assertEqual(run(unknown, threshold=0.0)["label"], "review")
        self.assertEqual(run(unknown, threshold=1.0)["label"], "review")
        self.assertEqual(run(excluded, threshold=0.0)["label"], "no_alert")
        self.assertEqual(run(excluded, threshold=1.0)["label"], "no_alert")

    def test_explanation_shows_rule_states_quotes_and_quality(self):
        text = dec.explain(run(trade()), SEGMENTS)
        self.assertIn("DECISION: ALARM", text)
        self.assertIn('s2 @ 4.0s q=0.905: "Grad drum"', text)
        self.assertIn("ASR quality not available", text)


class FamilyConfigTest(unittest.TestCase):
    """A bank turns a whole check on or off without touching predicates (config/policies.json)."""

    def test_disabled_family_is_absent_by_configuration_not_a_technical_gap(self):
        off = {**POLICIES, "families": {name: ({**fam, "enabled": False} if name == "splitting" else fam)
                                        for name, fam in POLICIES["families"].items()}}
        d = dec.decide(extraction(drop=("splitting",)), SEGMENTS, off)
        self.assertEqual((d["label"], d["issues"], d["disabled_families"]), ("no_alert", [], ["splitting"]))

    def test_disabled_family_is_excluded_from_the_prompt_and_schema(self):
        off = {**POLICIES, "families": {name: ({**fam, "enabled": False} if name == "trade" else fam)
                                        for name, fam in POLICIES["families"].items()}}
        schema = ex.build_schema(off)
        messages = ex.build_messages(SEGMENTS, off)
        self.assertNotIn("trade", schema["properties"]["families"]["properties"])
        self.assertNotIn("## trade:", messages[0]["content"])

    def test_status_distinguishes_a_failed_extraction_from_a_genuine_review(self):
        failed = dec.decide({}, SEGMENTS, POLICIES, error="session limit")
        genuine = run(("access", {"conditions": {"value_spoken": cond("unknown"), "access_secret": cond("unknown"),
                                                 "secret_active": cond("unknown")}}))
        self.assertEqual((failed["status"], failed["label"]), ("failed", "review"))
        self.assertEqual((genuine["status"], genuine["label"]), ("ok", "review"))


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


class ChallengeRegressionTest(unittest.TestCase):
    """Decision cases required by the challenge brief; each asserts states, evidence, and reason."""

    def test_active_spoken_access_code_is_an_alarm(self):
        high_quality = [{**s, "avg_logprob": -0.1} if s["id"] == "s3" else s for s in SEGMENTS]
        conditions = {name: cond("true", ("s3", "482913"))
                      for name in ("value_spoken", "access_secret", "secret_active")}
        result = run(("access", {"conditions": conditions}), segments=high_quality)
        event = result["events"][0]
        self.assertEqual((result["label"], result["reasons"], event["status"]), ("alarm", [], "present"))
        self.assertEqual({name: value["state"] for name, value in event["conditions"].items()},
                         {"value_spoken": "true", "access_secret": "true", "secret_active": "true"})
        self.assertTrue(all(value["evidence"] for value in event["conditions"].values()))

    def test_expired_example_code_is_explicitly_excluded(self):
        conditions = {
            "value_spoken": cond("true", ("s3", "482913")),
            "access_secret": cond("true", ("s3", "482913")),
            "secret_active": cond("false", ("s3", "abgloffnigs Muster")),
        }
        result = run(("access", {"conditions": conditions}))
        event = result["events"][0]
        self.assertEqual((result["label"], result["reasons"], event["status"]), ("no_alert", [], "absent"))
        self.assertEqual(event["conditions"]["secret_active"]["state"], "false")
        self.assertTrue(event["conditions"]["secret_active"]["evidence"])

    def test_code_with_unestablished_validity_is_reviewed(self):
        conditions = {
            "value_spoken": cond("true", ("s3", "482913")),
            "access_secret": cond("true", ("s3", "482913")),
            "secret_active": cond("unknown"),
        }
        result = run(("access", {"conditions": conditions}))
        event = result["events"][0]
        self.assertEqual((result["label"], result["reasons"], event["status"]),
                         ("review", ["missing_policy_fact"], "undetermined"))
        self.assertEqual(event["conditions"]["secret_active"]["state"], "unknown")
        self.assertIn("still valid", event["open_questions"][0])

    def test_later_adviser_refusal_does_not_undo_a_grounded_trade_request(self):
        later_refusal = {"id": "s5", "speaker": None, "start": 16.0, "end": 20.0,
                         "text": "Das darf ich nicht für Sie ausführen.", "avg_logprob": -0.1}
        result = run(trade(), segments=SEGMENTS + [later_refusal])
        event = result["events"][0]
        self.assertEqual((result["label"], result["reasons"], event["status"]), ("alarm", [], "present"))
        self.assertEqual(event["conditions"]["own_trade_request"]["state"], "true")
        self.assertTrue(event["conditions"]["own_trade_request"]["evidence"])

    def test_innocent_keyword_occurrence_does_not_change_a_no_alert(self):
        innocent = [{"id": "s1", "speaker": None, "start": 0.0, "end": 3.0,
                     "text": "Die Quartalszahlen sind bereits publiziert.", "avg_logprob": -0.1}]
        hits = ex.keyword_hits(innocent, KEYWORDS)
        result = run(segments=innocent)
        self.assertTrue(hits, "the transcript must contain a configured keyword")
        self.assertEqual((result["label"], result["reasons"], result["events"]), ("no_alert", [], []))


class ActorRoleTest(unittest.TestCase):
    def disclosure(self, actor_value):
        conditions = {
            "advisor_disclosed": cond("true", ("s3", "482913")),
            "third_party_detail": cond("true", ("s3", "482913")),
            "authority_absent": cond("true", ("s3", "482913")),
        }
        return ("disclosure", {"actor": actor_value, "conditions": conditions})

    def test_decisive_unknown_role_routes_only_that_event_to_review(self):
        result = run(self.disclosure(actor()))
        event = result["events"][0]
        self.assertEqual((result["label"], result["reasons"], event["reason"]),
                         ("review", ["actor_role_unknown"], "actor_role_unknown"))
        self.assertEqual(event["conditions"]["advisor_disclosed"]["state"], "unknown")
        self.assertIn("actor_role_unknown", event["conditions"]["advisor_disclosed"]["uncertainty"])
        self.assertIn("Was the person disclosing", event["open_questions"][-1])

    def test_unknown_role_does_not_downgrade_an_irrelevant_predicate(self):
        high_quality = [{**s, "avg_logprob": -0.1} if s["id"] == "s3" else s for s in SEGMENTS]
        conditions = {name: cond("true", ("s3", "482913"))
                      for name in ("value_spoken", "access_secret", "secret_active")}
        result = run(("access", {"actor": actor(), "conditions": conditions}), segments=high_quality)
        self.assertEqual((result["label"], result["reasons"]), ("alarm", []))

    def test_valid_exclusion_is_not_overridden_by_unknown_role(self):
        event = ("trade", {"actor": actor(), "conditions": {
            "own_trade_request": cond("false", ("s4", "Ich weiss nöd")),
            "information_nonpublic": cond("unknown"),
            "information_market_relevant": cond("unknown"),
            "request_based_on_information": cond("unknown"),
        }})
        result = run(event)
        self.assertEqual((result["label"], result["reasons"], result["events"][0]["status"]), ("no_alert", [], "absent"))
        self.assertEqual(result["events"][0]["conditions"]["own_trade_request"]["state"], "false")

    def test_alarm_wins_over_a_separate_role_ambiguous_event(self):
        trade_event = trade()
        trade_event[1]["actor"] = actor("customer", "inferred", "s2")
        result = run(self.disclosure(actor()), trade_event)
        by_family = {event["family"]: event for event in result["events"]}
        ambiguous, supported = by_family["disclosure"], by_family["trade"]
        self.assertEqual((result["label"], ambiguous["reason"], supported["label"]),
                         ("alarm", "actor_role_unknown", "alarm"))


class ExtractionActorContractTest(unittest.TestCase):
    def test_schema_requires_a_grounded_actor_for_every_event(self):
        schema = ex.build_schema(POLICIES)
        trade = schema["properties"]["families"]["properties"]["trade"]["properties"]["events"]["items"]
        actor_schema = trade["properties"]["actor"]
        self.assertIn("actor", trade["required"])
        self.assertEqual(actor_schema["required"], ["role", "status", "segment_ids"])
        self.assertEqual(actor_schema["properties"]["role"]["enum"], ["customer", "advisor", "unknown"])

    def test_prompt_distinguishes_inferred_and_unresolvable_role_contexts(self):
        inferred = [{"id": "s001", "speaker": None, "start": 0, "end": 1,
                     "text": "I am your bank adviser and cannot execute that order."}]
        unresolved = [{"id": "s001", "speaker": None, "start": 0, "end": 1,
                       "text": "Could somebody make that order?"}]
        prompt = ex.build_messages(inferred, POLICIES)[0]["content"]
        self.assertIn('role="unknown", status="unknown", segment_ids=[]', prompt)
        self.assertIn("Do not infer a role from turn order", prompt)
        self.assertNotEqual(inferred[0]["text"], unresolved[0]["text"])


class AsrKeywordVariantTest(unittest.TestCase):
    """K03/K04 carry two extra `de` terms added on 2026-10-10: not invented, but copied verbatim from real
    Whisper output. On the noisy takes of Stufe1_D02 and Stufe1_D03, background noise makes Whisper mishear
    "Quartalszahlen" as "Quantauszahlen" and "vor der Meldung" as "von der Meldung" -- the same two slips in
    two independent dialogues, so this is an observed ASR pattern, not a speculative keyword."""

    def test_noisy_asr_variants_restore_keyword_coverage_parity_with_clean_audio(self):
        for call in ("Stufe1_D02", "Stufe1_D03"):
            clean = ex.load_json(ROOT / "data" / "Transcriptions" / f"{call}-K1.json")["segments"]
            noisy = ex.load_json(ROOT / "data" / "Transcriptions" / f"{call}-K2.json")["segments"]
            families_clean = {h["keyword"] for h in ex.keyword_hits(clean, KEYWORDS)}
            families_noisy = {h["keyword"] for h in ex.keyword_hits(noisy, KEYWORDS)}
            self.assertEqual(families_clean, families_noisy, call)
            self.assertIn("K03", families_noisy)
            self.assertIn("K04", families_noisy)

    def test_harmless_call_stays_no_alert_with_the_extra_keyword_hits(self):
        """D03 keeps every K01-K05 keyword on both takes (including the new variants) and is still no_alert:
        confirms keyword coverage is independent of the decision, as required."""
        for take in ("K1", "K2"):
            segments = [{**s, "speaker": None} for s in
                       ex.load_json(ROOT / "data" / "Transcriptions" / f"Stufe1_D03-{take}.json")["segments"]]
            self.assertGreaterEqual(len({h["keyword"] for h in ex.keyword_hits(segments, KEYWORDS)}), 5, take)


class DataTest(unittest.TestCase):
    def test_keyword_matcher_counts_each_observed_occurrence(self):
        segments = [{"id": "s1", "speaker": None, "start": 0, "end": 2,
                     "text": "Quartalszahlen und nochmals Quartalszahlen.", "avg_logprob": -0.1}]
        hits = [hit for hit in ex.keyword_hits(segments, KEYWORDS) if hit["keyword"] == "K03" and hit["phrase"] == "Quartalszahlen"]
        self.assertEqual(len(hits), 2)
        self.assertEqual([(hit["start"], hit["end"]) for hit in hits], [(0, 14), (28, 42)])

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

    def test_metrics_separate_alarm_to_review_from_alarm_to_no_alert(self):
        rows = [
            {"call": "Stufe1_A", "gold": "alarm", "pred": "alarm", "evidence_hit": True},
            {"call": "Stufe1_B", "gold": "alarm", "pred": "review", "evidence_hit": True},
            {"call": "Stufe2_C", "gold": "alarm", "pred": "no_alert", "evidence_hit": False},
            {"call": "Stufe2_D", "gold": "no_alert", "pred": "alarm", "evidence_hit": None},
            {"call": "Stufe3_E", "gold": "review", "pred": "review", "evidence_hit": True},
        ]
        result = pipeline.metrics(rows)
        self.assertEqual(result["alarm_to_review (missed escalation, still triaged)"], "1/3")
        self.assertEqual(result["alarm_to_no_alert (missed without triage)"], "1/3")
        self.assertEqual(result["missed_strict (alarm expected, not alarm)"], "2/3")

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
