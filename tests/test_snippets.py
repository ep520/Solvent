"""Snippet mode: what the extraction model reads, how it is cached, and how the dashboard finds it. No data folder needed."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from callguard import extract as ex  # noqa: E402
from callguard import pipeline  # noqa: E402
from callguard import snippets as S  # noqa: E402
from callguard import ui  # noqa: E402

POLICIES = ex.load_json(ROOT / "config" / "policies.json")
KEYWORDS = {"families": {"trade": {"keywords": ["K01", "K04"]}},
            "keywords": [{"id": "K01", "label": "Nicht öffentliche Information", "enabled": True,
                          "de": ["nicht veröffentlicht"], "gsw": ["no nöd publiziert"]},
                         {"id": "K04", "label": "Handeln vor der Meldung", "enabled": True,
                          "de": ["vor der Meldung"], "gsw": ["vor de Meldig"]}]}
FILLER = [f"Mir händ no über s Wetter und d Ferie gredt, Teil {i}." for i in range(12)]
SEGMENTS = ([{"id": f"s{i:03d}", "speaker": None, "start": 4.0 * i, "end": 4.0 * i + 3.5, "text": t, "avg_logprob": -0.2}
             for i, t in enumerate(FILLER[:6], 1)] +
            [{"id": "s007", "speaker": None, "start": 24.0, "end": 28.0, "avg_logprob": -0.1,
              "text": "D Quartalszahle sind no nöd publiziert, das weiss ich vo innen."},
             {"id": "s008", "speaker": None, "start": 28.0, "end": 32.0, "avg_logprob": -0.4,
              "text": "Drum möcht ich jetzt chaufe, vor de Meldig."}] +
            [{"id": f"s{i:03d}", "speaker": None, "start": 4.0 * i, "end": 4.0 * i + 3.5, "text": t, "avg_logprob": -0.2}
             for i, t in enumerate(FILLER[6:], 9)])
NO_CUES = {**S.DEFAULTS, "cues": ""}


class SnippetViewTest(unittest.TestCase):
    def test_keyword_turns_are_selected_with_scores_and_spans(self):
        v = S.view(SEGMENTS, KEYWORDS, NO_CUES)
        self.assertEqual(v["mode"], "snippets")
        sn = v["snippets"][0]
        self.assertIn("s007", sn["anchor_segment_ids"])
        self.assertIn("s008", sn["anchor_segment_ids"])
        self.assertNotIn("s001", sn["segment_ids"])            # far filler is not sent
        hit = next(h for h in sn["hits"] if h["keyword_id"] == "K01")
        self.assertEqual((hit["how"], hit["score"], hit["turn"], hit["label"]), ("exact", 1.0, "s007", "Nicht öffentliche Information"))
        turn = next(t for t in sn["turns"] if t["tid"] == "s007")
        self.assertEqual(turn["text"][turn["spans"][0]["start"]:turn["spans"][0]["end"]], "no nöd publiziert")
        self.assertEqual(turn["quality"], 0.905)
        self.assertLess(v["stats"]["kept_text_pct"], 50)
        json.dumps(v)                                           # stored in the cache as JSON

    def test_render_uses_segment_ids_bold_and_hits(self):
        text = S.render(S.view(SEGMENTS, KEYWORDS, NO_CUES))
        self.assertIn("[s007]", text)
        self.assertIn("**no nöd publiziert**", text)
        self.assertIn("keyword K01 Nicht öffentliche Information", text)
        self.assertIn("ASR 0.905", text)
        self.assertNotIn("Teil 0", text)

    def test_no_hit_falls_back_to_the_full_transcript(self):
        quiet = [s for s in SEGMENTS if s["id"] not in ("s007", "s008")]
        v = S.view(quiet, KEYWORDS, NO_CUES)
        self.assertEqual(v["mode"], "full_transcript")
        self.assertIn("nothing is dropped unseen", v["reason"])
        self.assertEqual(ex.build_messages(quiet, POLICIES, v), ex.build_messages(quiet, POLICIES))

    def test_disabled_config_is_the_old_prompt(self):
        v = S.view(SEGMENTS, KEYWORDS, {**NO_CUES, "enabled": False})
        self.assertEqual(ex.build_messages(SEGMENTS, POLICIES, v), ex.build_messages(SEGMENTS, POLICIES))

    def test_stdlib_fuzzy_fallback_has_the_rapidfuzz_scale(self):
        from difflib import SequenceMatcher
        self.assertGreaterEqual(100 * SequenceMatcher(None, "quartalszahlen", "quartalszahle").ratio(), S.FUZZY_MIN)


class TranscriptFormatTest(unittest.TestCase):
    def test_m_file_turns_are_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.txt"
            path.write_text("M01 Rückruf\nKundschaft: Person A\n\nT001 B: Grüezi, Rosenau Bank.\nT002 K: Ich wett chaufe.\n",
                            encoding="utf-8")
            segs = ex.segments_from_transcript(path)
        self.assertEqual([(s["id"], s["speaker"], s["text"]) for s in segs],
                         [("T001", "Beratung", "Grüezi, Rosenau Bank."), ("T002", "Kunde", "Ich wett chaufe.")])

    def test_unknown_speaker_does_not_block_customer_cues(self):
        self.assertEqual(S.turns_from_segments([{"id": "s1", "speaker": None, "text": "x"}])[0].speaker, "unknown")


class SnippetExtractionTest(unittest.TestCase):
    def setUp(self):
        self.view = S.view(SEGMENTS, KEYWORDS, NO_CUES)
        self.profile = {"name": "fake", "model": "m"}

    def test_prompt_has_snippet_rules_and_no_full_transcript(self):
        system, user = ex.build_messages(SEGMENTS, POLICIES, self.view)
        self.assertIn("SELECTED PASSAGES", system["content"])
        self.assertNotIn("read the whole call before answering", system["content"])
        self.assertTrue(user["content"].startswith("Selected passages of the call"))
        self.assertNotEqual(ex.cache_path(SEGMENTS, POLICIES, self.profile, view=self.view),
                            ex.cache_path(SEGMENTS, POLICIES, self.profile))

    def test_bold_markers_are_removed_from_quotes_and_view_is_stored(self):
        reply = {"families": {"trade": {"events": [{"conditions": {"information_nonpublic": {
            "state": "true", "evidence": [{"segment_id": "s007", "quote": "sind **no nöd publiziert**"}]}}}]}}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ex.models, "check", return_value=self.profile), \
                mock.patch.object(ex.models, "chat_json", return_value=reply) as chat:
            out = ex.extract(SEGMENTS, POLICIES, cache_dir=tmp, view=self.view)
            again = ex.extract(SEGMENTS, POLICIES, cache_dir=tmp, view=self.view)
            pointer = ex.latest_path(SEGMENTS, POLICIES, self.profile, tmp)
            self.assertTrue(pointer.exists())
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(out, again)
        quote = out["families"]["trade"]["events"][0]["conditions"]["information_nonpublic"]["evidence"][0]["quote"]
        self.assertEqual(quote, "sind no nöd publiziert")
        self.assertEqual(out["_meta"]["input"]["mode"], "snippets")
        self.assertTrue(out["_meta"]["prompt_version"].endswith("+" + S.VERSION))

    def test_skip_model_when_nothing_is_selected(self):
        quiet = [s for s in SEGMENTS if s["id"] not in ("s007", "s008")]
        v = S.view(quiet, KEYWORDS, {**NO_CUES, "if_no_snippets": "skip_model"})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ex.models, "check", return_value=self.profile), \
                mock.patch.object(ex.models, "chat_json") as chat:
            out = ex.extract(quiet, POLICIES, cache_dir=tmp, view=v)
        chat.assert_not_called()
        self.assertEqual(out["families"]["trade"], {"events": []})


class DashboardInputTest(unittest.TestCase):
    def test_dashboard_finds_current_stale_and_full_extractions(self):
        profile = {"name": "fake", "model": "m"}
        view = S.view(SEGMENTS, KEYWORDS, NO_CUES)
        with tempfile.TemporaryDirectory() as cache, mock.patch.object(ex.models, "check", return_value=profile), \
                mock.patch.object(ex.models, "chat_json", return_value={"families": {}}):
            self.assertEqual(ui.find_extraction(SEGMENTS, POLICIES, profile, view, cache), (None, None))
            full = ex.extract(SEGMENTS, POLICIES, cache_dir=cache)                 # before snippet mode
            self.assertEqual(ui.find_extraction(SEGMENTS, POLICIES, profile, view, cache)[1], "full_transcript")
            Path(cache, "latest").rename(Path(cache, "old-pointers"))
            self.assertEqual(ui.find_extraction(SEGMENTS, POLICIES, profile, view, cache)[1], "full_transcript")
            ex.extract(SEGMENTS, POLICIES, cache_dir=cache, view=view)
            self.assertEqual(ui.find_extraction(SEGMENTS, POLICIES, profile, view, cache)[1], "current")
            edited = S.view(SEGMENTS, {**KEYWORDS, "keywords": KEYWORDS["keywords"][:1]}, NO_CUES)
            path, status = ui.find_extraction(SEGMENTS, POLICIES, profile, edited, cache)
            self.assertEqual(status, "stale_input")
            shown = ui.model_input(ex.load_json(path), edited, SEGMENTS)
            self.assertEqual(shown["mode"], "full_transcript")
            self.assertEqual(shown["highlights"], edited["snippets"])
            self.assertEqual(ui.model_input(full, view, SEGMENTS)["mode"], "full_transcript")

    def test_old_full_transcript_cache_is_shown_as_such(self):
        shown = ui.model_input({"families": {}, "_meta": {}}, None, SEGMENTS)
        self.assertEqual((shown["mode"], shown["segmentCount"]), ("full_transcript", len(SEGMENTS)))


class FullTranscriptMvpTest(unittest.TestCase):
    def test_pipeline_extracts_the_full_transcript_and_keeps_snippets_as_highlights(self):
        extraction = {"families": {}, "_meta": {}}
        decision = {"label": "no_alert", "events": [], "reasons": [], "status": "ok", "issues": []}
        with mock.patch.object(ex, "extract", return_value=(extraction, False)) as extract_call, \
                mock.patch.object(pipeline.dec, "decide", return_value=decision):
            result, _ = pipeline.run_segments("call-1", "source", SEGMENTS, POLICIES, KEYWORDS, None, None,
                                              speakers=False, audio=True)
        self.assertIsNone(extract_call.call_args.kwargs["view"])
        self.assertEqual(result["input"]["mode"], "full_transcript")
        self.assertTrue(result["highlights"]["snippets"])


if __name__ == "__main__":
    unittest.main()
