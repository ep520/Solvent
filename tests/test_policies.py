"""Policies against the real dataset: hand-written oracle extractions and replayed real model extractions."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from callguard import decide as dec  # noqa: E402
from callguard import extract as ex  # noqa: E402
from callguard import pipeline  # noqa: E402

POLICIES = ex.load_json(ROOT / "config" / "policies.json")
ORACLE = {k: v for k, v in ex.load_json(ROOT / "tests" / "fixtures" / "oracle.json").items() if not k.startswith("_")}
REPLAY_DIR = ROOT / "tests" / "fixtures" / "replay" / "claude-sonnet-5" / "extract-1"
# Real model errors still present; remove an entry once a prompt/policy change fixes it.
KNOWN_MISSES = {
    ("Stufe3_C19", "speakers"): "fact_relevant set true although the customer cannot say which line is meant",
    ("Stufe3_C19", "nospeakers"): "same as above",
    ("Stufe3_C20", "nospeakers"): "evasion_purpose set false from 'I cannot say why' (should be unknown)",
}


def segments(call, speakers=True):
    return ex.segments_from_transcript(ROOT / "data" / "Transkript" / f"{call}.txt", speakers=speakers)


def by_family(events):
    """Wrap a flat list of events into the per-family extraction format (every family present)."""
    families = {name: {"events": []} for name in POLICIES["families"]}
    for e in events:
        families[e["family"]]["events"].append({k: v for k, v in e.items() if k != "family"})
    return {"families": families, "_meta": {}}


def oracle_extraction(events):
    def refs(pair):
        return [{"segment_id": pair[0], "quote": pair[1]}] if pair else []
    return by_family([{"family": e["family"], "object_type": e.get("object_type"), "evidence": refs(e.get("evidence")),
                       "conditions": {n: {"state": v[0], "evidence": refs(v[1:])} for n, v in e.get("conditions", {}).items()}}
                      for e in events])


def from_extract1(extraction):
    """extract-1 fixtures hold a flat 'events' list; the old schema had no per-family completeness."""
    return by_family(extraction.get("events", []))


class ConfigTest(unittest.TestCase):
    def test_every_alarming_family_has_conditions_and_numbers_cannot_alarm(self):
        for name, fam in POLICIES["families"].items():
            if fam.get("can_alarm", True):
                self.assertTrue(fam["conditions"], name)
        self.assertFalse(POLICIES["families"]["numbers"]["can_alarm"])

    def test_fixtures_use_exactly_the_configured_conditions(self):
        for call, events in ORACLE.items():
            for e in events:
                self.assertEqual(set(e.get("conditions", {})), set(POLICIES["families"][e["family"]]["conditions"]), call)


class OracleTest(unittest.TestCase):
    """If the extractor were perfect, do the policies give the expected assessment on real calls?"""

    def test_oracle_extractions_reach_the_expected_assessment(self):
        for call, events in ORACLE.items():
            with self.subTest(call=call):
                g = pipeline.gold(call)
                d = dec.decide(oracle_extraction(events), segments(call), POLICIES)
                self.assertEqual(d["label"], g["label"])
                self.assertEqual([i for e in d["events"] for i in e["issues"]], [], "oracle quote not grounded")
                self.assertTrue(pipeline.cited_segments(d) & set(g["evidence"]), "no expected evidence turn cited")
                self.assertEqual({e["family"] for e in events}, {g["family"]})
                if g["label"] == "review":
                    self.assertEqual(d["reasons"], ["missing_policy_fact"])

    def test_every_transcript_is_covered(self):
        self.assertEqual(set(ORACLE), {p.stem for p in (ROOT / "data" / "Transkript").glob("*.txt")})

    def test_refusal_is_not_disclosure_and_events_stay_separate(self):
        d = dec.decide(oracle_extraction(ORACLE["Stufe1_D06"]), segments("Stufe1_D06"), POLICIES)
        self.assertEqual([e["status"] for e in d["events"]], ["absent", "absent"])
        self.assertEqual(d["events"][0]["conditions"]["value_spoken"]["state"], "false")

    def test_numbers_keep_their_object_classification(self):
        for call in ("Stufe2_C01", "Stufe2_C02", "Stufe2_C03", "Stufe2_C04"):
            d = dec.decide(oracle_extraction(ORACLE[call]), segments(call), POLICIES)
            self.assertEqual(d["events"][0]["object_type"], ORACLE[call][0]["object_type"])

    def test_context_pairs_differ_only_by_the_deciding_condition(self):
        pairs = [("Stufe2_C05", "Stufe2_C06", "information_nonpublic"), ("Stufe2_C09", "Stufe2_C10", "secret_active"),
                 ("Stufe2_C11", "Stufe2_C12", "authority_absent"), ("Stufe2_C16", "Stufe2_C15", "movements_connected"),
                 ("Stufe1_D02", "Stufe1_D03", "own_trade_request"), ("Stufe2_C09", "Stufe1_D06", "secret_active"),
                 ("Stufe1_D09", "Stufe3_C19", "fact_relevant")]
        for a, b, deciding in pairs:
            ca, cb = ORACLE[a][-1]["conditions"], ORACLE[b][-1]["conditions"]
            self.assertNotEqual(ca[deciding][0], cb[deciding][0], (a, b))
            self.assertNotEqual(pipeline.gold(a)["label"], pipeline.gold(b)["label"], (a, b))


class ReplayTest(unittest.TestCase):
    """Real Claude Sonnet 5 extractions (frozen with `pipeline freeze`) replayed through the current policies."""

    def test_all_21_transcripts_in_both_modes_are_frozen(self):
        self.assertEqual(len(list(REPLAY_DIR.glob("*.json"))), 42)

    def test_replayed_extractions_reach_the_expected_assessment(self):
        for path in sorted(REPLAY_DIR.glob("*.json")):
            call, mode = path.stem.split(".")
            with self.subTest(call=call, mode=mode):
                extraction = from_extract1(json.loads(path.read_text(encoding="utf-8")))
                d = dec.decide(extraction, segments(call, speakers=mode == "speakers"), POLICIES)
                gold = pipeline.gold(call)["label"]
                if (call, mode) in KNOWN_MISSES:
                    self.assertNotEqual(d["label"], gold, f"now correct: remove {(call, mode)} from KNOWN_MISSES")
                else:
                    self.assertEqual(d["label"], gold)


if __name__ == "__main__":
    unittest.main()
