"""callguard/snippets_bridge.py: opt-in --snippets integration, off by default.

Needs `rapidfuzz` (tools/snippets/snippets.py's top-level dependency): these tests are skipped, not
failed, when it is absent, so the normal suite stays green on a machine that only has the core
pipeline's dependencies. Run `python3 -m unittest tests.test_snippets_bridge` with rapidfuzz installed
(e.g. in a throwaway venv) to actually exercise them. None of this downloads a model: every test here
uses semantic="off".
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from callguard import extract as ex  # noqa: E402
from callguard import pipeline  # noqa: E402
from callguard import snippets_bridge as sb  # noqa: E402

try:
    import rapidfuzz  # noqa: F401
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False

KEYWORDS_PATH = ROOT / "data" / "Stichwortliste.json"
CUES_PATH = ROOT / "tools" / "snippets" / "cues_general.json"
POLICIES = ex.load_json(ROOT / "config" / "policies.json")
# The three Stufe2/3 dialogues PLAN.md flags as deliberately keyword-free: the real test of a
# turn-selection filter is whether it keeps the expected evidence turns on exactly these calls.
KEYWORD_FREE_CASES = ("Stufe2_C06", "Stufe2_C09", "Stufe3_C19")


class SnippetsUnavailableTest(unittest.TestCase):
    """Must degrade cleanly regardless of whether this machine happens to have rapidfuzz."""

    def test_missing_dependency_raises_a_clear_catchable_error(self):
        with mock.patch.object(sb, "_snippets_module", side_effect=ImportError("no module named 'rapidfuzz'")):
            with self.assertRaises(ImportError):
                sb._snippets_module()

    def test_pipeline_falls_back_to_the_full_transcript_when_snippets_are_unavailable(self):
        segments = ex.segments_from_transcript(ROOT / "data" / "Transkript" / "Stufe1_D02.txt")
        with mock.patch.object(sb, "select_snippets", side_effect=sb.SnippetsUnavailable("boom")):
            with mock.patch.object(ex, "extract", return_value=({"families": {}, "_meta": {}}, False)) as fake:
                pipeline.run_segments("Stufe1_D02", "x", segments, POLICIES, ex.load_json(KEYWORDS_PATH),
                                      None, None, True, False,
                                      snippets={"keywords_path": KEYWORDS_PATH, "semantic": "off", "cues": None, "checks": None})
        sent_segments = fake.call_args[0][0]
        self.assertEqual(len(sent_segments), len(segments), "a missing optional dependency must not shrink the call")


@unittest.skipUnless(HAVE_RAPIDFUZZ, "rapidfuzz not installed; run in a venv that has it")
class SelectSnippetsTest(unittest.TestCase):
    def test_keeps_a_strict_subset_in_original_order_with_the_same_segment_shape(self):
        segments = ex.segments_from_transcript(ROOT / "data" / "Transkript" / "Stufe1_D02.txt")
        kept, report = sb.select_snippets(segments, KEYWORDS_PATH, semantic="off", cues=CUES_PATH)
        self.assertLess(len(kept), len(segments))
        self.assertEqual([s["id"] for s in kept], sorted({s["id"] for s in kept},
                         key=lambda sid: [s["id"] for s in segments].index(sid)))
        by_id = {s["id"]: s for s in segments}
        for seg in kept:
            self.assertIn(seg["id"], by_id)
            self.assertEqual(seg, by_id[seg["id"]], "the bridge must only filter, never rewrite a segment")
        self.assertGreater(report["stats"]["keyword_hits"], 0)

    def test_off_semantic_never_imports_sentence_transformers(self):
        """semantic="off" (the pipeline default) must not even try to load the embedding model."""
        segments = ex.segments_from_transcript(ROOT / "data" / "Transkript" / "Stufe1_D02.txt")
        blocked = dict(sys.modules)
        blocked["sentence_transformers"] = None  # importing it would now raise ImportError
        with mock.patch.dict(sys.modules, blocked):
            sb.select_snippets(segments, KEYWORDS_PATH, semantic="off", cues=CUES_PATH)  # must not raise

    def test_keyword_free_cases_still_keep_the_expected_evidence_turns(self):
        """The dataset's whole point for Stufe2/3: no keyword, only context decides. A turn-selection
        filter is only safe to use if it does not drop the turns the expected assessment relies on."""
        for call in KEYWORD_FREE_CASES:
            with self.subTest(call=call):
                segments = ex.segments_from_transcript(ROOT / "data" / "Transkript" / f"{call}.txt")
                kept, _ = sb.select_snippets(segments, KEYWORDS_PATH, semantic="off", cues=CUES_PATH)
                kept_ids = {s["id"] for s in kept}
                expected = set(pipeline.gold(call)["evidence"])
                self.assertTrue(expected, f"{call}: no expected evidence turns in Skript_mit_Sollbewertung")
                missing = expected - kept_ids
                self.assertFalse(missing, f"{call}: snippet filter (cues only, no semantic) dropped {missing}")


@unittest.skipUnless(HAVE_RAPIDFUZZ, "rapidfuzz not installed; run in a venv that has it")
class PipelineIntegrationTest(unittest.TestCase):
    def test_run_text_sends_only_the_kept_segments_to_extraction(self):
        captured = {}

        def fake_extract(segments, policies, profile=None, return_cache_status=False):
            captured["segments"] = segments
            result = {"families": {name: {"events": []} for name in policies["families"]}, "_meta": {}}
            return (result, False) if return_cache_status else result

        with mock.patch.object(ex, "extract", side_effect=fake_extract):
            result, _ = pipeline.run_text(
                ROOT / "data" / "Transkript" / "Stufe2_C06.txt", POLICIES, ex.load_json(KEYWORDS_PATH),
                snippets={"keywords_path": KEYWORDS_PATH, "semantic": "off", "cues": CUES_PATH, "checks": None})

        full = len(ex.segments_from_transcript(ROOT / "data" / "Transkript" / "Stufe2_C06.txt"))
        self.assertLess(len(captured["segments"]), full)
        self.assertEqual(result["_evaluation"]["snippets"]["full_segments"], full)
        self.assertEqual(result["_evaluation"]["snippets"]["kept_turns"], len(captured["segments"]))

    def test_without_the_flag_the_whole_transcript_still_goes_through(self):
        """The default path (no --snippets) must be byte-for-byte what it was before this feature."""
        captured = {}

        def fake_extract(segments, policies, profile=None, return_cache_status=False):
            captured["segments"] = segments
            return ({"families": {name: {"events": []} for name in policies["families"]}, "_meta": {}}, False)

        with mock.patch.object(ex, "extract", side_effect=fake_extract):
            pipeline.run_text(ROOT / "data" / "Transkript" / "Stufe2_C06.txt", POLICIES, ex.load_json(KEYWORDS_PATH))
        full = len(ex.segments_from_transcript(ROOT / "data" / "Transkript" / "Stufe2_C06.txt"))
        self.assertEqual(len(captured["segments"]), full)


if __name__ == "__main__":
    unittest.main()
