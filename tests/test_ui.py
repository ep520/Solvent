import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from callguard import decide as dec  # noqa: E402
from callguard import evidence  # noqa: E402
from callguard import extract as ex  # noqa: E402
from callguard import ui  # noqa: E402

POLICIES = ex.load_json(ROOT / "config" / "policies.json")
WAV = ROOT / "data" / "Audio" / "Stufe1_D02-K1.wav"
SEGMENTS = [
    {"id": "s001", "speaker": None, "start": 0.0, "end": 4.0, "text": "Die Quartalszahlen sind noch nicht publiziert.", "avg_logprob": -0.2},
    {"id": "s002", "speaker": None, "start": 4.0, "end": 8.0, "text": "Darum möchte ich die Aktie jetzt kaufen.", "avg_logprob": -0.4},
]


def get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers), e.read()
        finally:
            e.close()


def post(base, path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        try:
            return error.code, dict(error.headers), error.read()
        finally:
            error.close()


def trade_extraction():
    c = lambda sid, q: {"state": "true", "evidence": [{"segment_id": sid, "quote": q}]}
    fams = {name: {"events": []} for name in POLICIES["families"]}
    fams["trade"]["events"].append({"actor": {"role": "customer", "status": "inferred", "segment_ids": ["s002"]}, "conditions": {
        "own_trade_request": c("s002", "möchte ich die Aktie jetzt kaufen"),
        "information_nonpublic": c("s001", "noch nicht publiziert"),
        "information_market_relevant": c("s001", "Die Quartalszahlen"),
        "request_based_on_information": c("s002", "Darum möchte ich")}})
    return {"families": fams}


class ClipTest(unittest.TestCase):
    def test_clip_adds_ten_seconds_each_side_and_clamps_to_the_file(self):
        data, start, end = evidence.clip(WAV, 20.0, 25.0)
        self.assertEqual((start, end), (10.0, 35.0))
        _, start, _ = evidence.clip(WAV, 3.0, 4.0)
        self.assertEqual(start, 0.0)
        full_duration = evidence.duration(WAV)
        _, _, end = evidence.clip(WAV, full_duration - 3.0, full_duration - 1.0)
        self.assertEqual(end, full_duration)
        with wave.open(str(WAV)) as src:
            rate = src.getframerate()
        self.assertEqual(len(data) - 44, int(25 * rate) * 2)


class DashboardCallTest(unittest.TestCase):
    def test_canonical_results_map_to_dashboard_fields_per_threshold(self):
        results = {name: dec.decide(trade_extraction(), SEGMENTS, POLICIES, threshold=v)
                   for name, v in {"sensitive": 0.5, "balanced": 0.6, "conservative": 0.75}.items()}
        call = ui.dashboard_call("Stufe1_D02-K2", SEGMENTS, results, "balanced", 155.1)
        self.assertEqual(call["classificationByThreshold"], {"sensitive": "Alarm", "balanced": "Alarm", "conservative": "Review"})
        self.assertEqual(call["reasonByThreshold"]["conservative"], ["below_escalation_threshold"])
        self.assertEqual((call["family"], call["date"], call["time"], call["asrQuality"]), ("Trade", "Level 1", "noisy audio", 67))
        self.assertNotIn("confidence", call)
        self.assertEqual(call["actor"], {"role": "customer", "status": "inferred"})
        self.assertEqual([e["actorRole"] for e in call["evidence"]], [None, "customer"])
        self.assertEqual([e["segments"] for e in call["evidence"]], [["s001"], ["s002"]])
        self.assertEqual(call["evidence"][0]["segment_ids"], ["s001"])
        self.assertEqual((call["evidence"][0]["evidence_start"], call["evidence"][0]["evidence_end"],
                          call["evidence"][0]["clip_start"], call["evidence"][0]["clip_end"]),
                         (0.0, 4.0, 0.0, 14.0))
        self.assertEqual({c["state"] for c in call["conditions"]}, {"Supported"})
        self.assertTrue(all(c["reason"].startswith("The transcript says:") for c in call["conditions"]))
        self.assertEqual(len(call["transcript"]), 2)

    def test_supported_conditions_carry_the_policy_wording_for_explainability(self):
        """Hovering 'Supported' (or any state) must show which policy rule was checked, not just the quote."""
        results = {"balanced": dec.decide(trade_extraction(), SEGMENTS, POLICIES, threshold=0.6)}
        call = ui.dashboard_call("Stufe1_D02-K1", SEGMENTS, results, "balanced", 155.1)
        own_trade = next(c for c in call["conditions"] if c["label"] == "Own trade request")
        self.assertEqual(own_trade["policy"],
                         POLICIES["families"]["trade"]["conditions"]["own_trade_request"])
        self.assertTrue(all(c.get("policy") for c in call["conditions"]), "every condition needs its policy text")

    def test_evidence_timestamp_in_the_reason_is_minutes_seconds_not_raw_seconds(self):
        """A dashboard timestamp must read like the audio player's own clock (0:04), never '4s'."""
        results = {"balanced": dec.decide(trade_extraction(), SEGMENTS, POLICIES, threshold=0.6)}
        call = ui.dashboard_call("Stufe1_D02-K1", SEGMENTS, results, "balanced", 155.1)
        at_4s = next(c for c in call["conditions"] if "at 0:04" in c["reason"] or " at 4s" in c["reason"])
        self.assertIn("at 0:04", at_4s["reason"])
        self.assertNotIn(" at 4s", at_4s["reason"])
        self.assertEqual(ui._format_timestamp(0), "0:00")
        self.assertEqual(ui._format_timestamp(65), "1:05")
        self.assertEqual(ui._format_timestamp(600), "10:00")

    def test_call_without_events_has_a_placeholder_evidence(self):
        empty = {"families": {name: {"events": []} for name in POLICIES["families"]}}
        results = {"balanced": dec.decide(empty, SEGMENTS, POLICIES, threshold=0.6)}
        call = ui.dashboard_call("Stufe2_C01-K3", SEGMENTS, results, "balanced", 160)
        self.assertEqual((call["classificationByThreshold"]["balanced"], call["family"]), ("No alert", "None"))
        self.assertEqual(call["evidence"][0]["segments"], [])
        self.assertEqual(call["actor"], {"role": "unknown", "status": "unknown"})
        self.assertTrue(call["noCandidateEvent"])
        self.assertIn("not a guarantee", call["factsReason"])

    def test_grounding_issues_are_preserved_for_a_review(self):
        extraction = trade_extraction()
        extraction["families"]["trade"]["events"][0]["conditions"]["information_nonpublic"]["evidence"][0]["quote"] = "invented passage"
        results = {"balanced": dec.decide(extraction, SEGMENTS, POLICIES, threshold=0.6)}
        call = ui.dashboard_call("Stufe1_D02-K1", SEGMENTS, results, "balanced", 155.1)
        self.assertEqual(call["classificationByThreshold"]["balanced"], "Review")
        self.assertTrue(any("grounding failed" in issue for issue in call["groundingIssues"]))


class ThresholdPresetTest(unittest.TestCase):
    """Switching the dashboard's threshold preset must never call a model: only re-run `decide` on cached facts."""

    def test_build_data_never_calls_the_chat_model(self):
        policies, keywords = ex.load_json(ROOT / "config" / "policies.json"), ex.load_json(ROOT / "data" / "Stichwortliste.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, transcripts = root / "Audio", root / "Transcriptions"
            audio.mkdir()
            transcripts.mkdir()
            (audio / "opaque-call.wav").write_bytes(b"fixture only")
            (transcripts / "opaque-call.json").write_text(json.dumps({"segments": SEGMENTS}), encoding="utf-8")
            cached = root / "extract.json"
            cached.write_text(json.dumps({"families": {name: {"events": []} for name in policies["families"]}}), encoding="utf-8")
            with unittest.mock.patch("callguard.models.chat_json", side_effect=AssertionError("must not call the model")), \
                    unittest.mock.patch.object(ui, "AUDIO", audio), \
                    unittest.mock.patch.object(ui, "TRANSCRIPTS", transcripts), \
                    unittest.mock.patch.object(ui, "find_extraction", return_value=(cached, "current")), \
                    unittest.mock.patch.object(ui.evidence, "duration", return_value=1.0):
                data = ui.build_data(policies, keywords)
        self.assertGreater(len(data["calls"]), 0)

    def test_every_preset_is_a_pure_recompute_of_the_same_cached_extraction(self):
        policies = ex.load_json(ROOT / "config" / "policies.json")
        data = ui.build_data(policies, ex.load_json(ROOT / "data" / "Stichwortliste.json"))
        presets = set(policies["escalation"]["presets"])
        for call in data["calls"]:
            self.assertEqual(set(call["classificationByThreshold"]), presets)
        sensitive, conservative = policies["escalation"]["presets"]["sensitive"], policies["escalation"]["presets"]["conservative"]
        self.assertGreater(conservative, sensitive)
        rank = {"No alert": 0, "Review": 1, "Alarm": 2}
        for call in data["calls"]:
            self.assertLessEqual(rank[call["classificationByThreshold"]["conservative"]],
                                 rank[call["classificationByThreshold"]["sensitive"]],
                                 f"{call['id']}: a stricter preset must never raise the classification")


class KeywordCoverageTest(unittest.TestCase):
    def setUp(self):
        self.keywords = ex.load_json(ROOT / "data" / "Stichwortliste.json")

    def test_configuration_and_observed_hits_are_reported_separately(self):
        hits = [{"keyword": "K01", "call": "call-a"}, {"keyword": "K01", "call": "call-a"},
                {"keyword": "K10", "call": "call-b"}]
        coverage = ui.keyword_coverage(self.keywords, hits, transcript_calls=4)
        self.assertEqual((coverage["families"], coverage["enabledKeywords"], coverage["configuredVariants"]), (6, 11, 58))
        self.assertEqual((coverage["occurrences"], coverage["callsWithOccurrences"], coverage["transcriptCalls"]), (3, 2, 4))
        trade = next(row for row in coverage["byFamily"] if row["id"] == "trade")
        self.assertEqual((trade["enabledKeywords"], trade["occurrences"]), (5, 2))

    def test_invalid_empty_or_duplicate_terms_are_rejected(self):
        broken = json.loads(json.dumps(self.keywords))
        broken["keywords"][0]["de"] = ["  "]
        with self.assertRaisesRegex(ValueError, "empty term"):
            ui.validate_keyword_config(broken)
        broken = json.loads(json.dumps(self.keywords))
        broken["keywords"][1]["de"].append(broken["keywords"][0]["de"][0])
        with self.assertRaisesRegex(ValueError, "duplicate term"):
            ui.validate_keyword_config(broken)

    def test_valid_config_is_saved_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keywords.json"
            saved = ui.save_keyword_config(self.keywords, path)
            self.assertEqual(ex.load_json(path), saved)


class KeywordDecisionIndependenceTest(unittest.TestCase):
    def test_suspicious_event_without_a_keyword_remains_an_alarm(self):
        no_match_list = {"keywords": [], "families": {}}
        result = dec.decide(trade_extraction(), SEGMENTS, POLICIES, threshold=0.6)
        self.assertEqual(ex.keyword_hits(SEGMENTS, no_match_list), [])
        self.assertEqual(result["label"], "alarm")


class PolicyConfigTest(unittest.TestCase):
    def test_policy_crud_validation_accepts_a_new_classification_only_policy(self):
        edited = json.loads(json.dumps(POLICIES))
        edited["families"]["new_identifier"] = {"title": "New identifier policy", "enabled": True,
                                                   "can_alarm": False, "conditions": {}, "role_requirements": {}}
        saved = ui.validate_policy_config(edited)
        self.assertFalse(saved["families"]["new_identifier"]["can_alarm"])

    def test_policy_validation_rejects_an_alarm_policy_without_conditions(self):
        edited = json.loads(json.dumps(POLICIES))
        edited["families"]["unsafe"] = {"title": "Unsafe", "enabled": True, "can_alarm": True, "conditions": {}}
        with self.assertRaisesRegex(ValueError, "needs at least one condition"):
            ui.validate_policy_config(edited)


class ReviewResolutionApiTest(unittest.TestCase):
    def test_only_a_review_can_be_saved_as_human_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = object.__new__(ui.Handler)
            handler.feedback_path = Path(directory) / "reviews.json"
            handler.corrections_dir = Path(directory) / "corrections"
            handler.policies_path = ROOT / "config" / "policies.json"
            handler.keywords_path = ROOT / "data" / "Stichwortliste.json"
            current = {"id": "call-1", "family": "Trade", "classificationByThreshold": {"balanced": "Review"},
                       "reasonByThreshold": {"balanced": ["missing_policy_fact"]}}
            with unittest.mock.patch.object(ui, "build_data", return_value={"calls": [current]}), \
                 unittest.mock.patch.object(ui.Handler, "transcript_segments", return_value=SEGMENTS):
                saved = handler.save_review({"call": "call-1", "threshold": "balanced", "outcome": "no_alert"})
                self.assertEqual(saved["outcome"], "no_alert")
                with self.assertRaisesRegex(ValueError, "only resolve"):
                    handler.save_review({"call": "call-1", "threshold": "sensitive", "outcome": "alarm"})


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keyword_path = Path(self.tmp.name) / "keywords.json"
        self.policies_path = Path(self.tmp.name) / "policies.json"
        self.corrections_dir = Path(self.tmp.name) / "corrections"
        self.feedback_path = Path(self.tmp.name) / "reviews.json"
        self.keyword_path.write_text((ROOT / "data" / "Stichwortliste.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.policies_path.write_text((ROOT / "config" / "policies.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.srv = ui.make_server("127.0.0.1", 0, policies_path=self.policies_path, keywords_path=self.keyword_path,
                                  corrections_dir=self.corrections_dir, feedback_path=self.feedback_path)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def test_serves_the_dashboard(self):
        status, headers, body = get(self.base, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"CallGuard", body)

    def test_audio_supports_range_requests(self):
        status, headers, body = get(self.base, "/audio/Stufe1_D02-K1.wav", {"Range": "bytes=0-99"})
        self.assertEqual((status, len(body), headers["Content-Range"]), (206, 100, f"bytes 0-99/{WAV.stat().st_size}"))

    def test_clip_endpoint_returns_a_wav_attachment(self):
        status, headers, body = get(self.base, "/clip/Stufe1_D02-K1.wav?start=40&end=45")
        self.assertEqual((status, headers["Content-Type"], body[:4]), (200, "audio/wav", b"RIFF"))
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_clip_endpoint_accepts_a_canonical_window_without_reapplying_context(self):
        status, headers, body = get(self.base, "/clip/Stufe1_D02-K1.wav?clip_start=10&clip_end=20")
        self.assertEqual((status, headers["Content-Type"], body[:4]), (200, "audio/wav", b"RIFF"))
        self.assertIn("10-20s", headers["Content-Disposition"])

    def test_paths_outside_the_dashboard_and_audio_are_refused(self):
        for path in ("/../config/policies.json", "/audio/..%2Fconfig.wav", "/audio/missing.wav", "/clip/Stufe1_D02-K1.wav"):
            self.assertIn(get(self.base, path)[0], (400, 404), path)

    def test_api_returns_thresholds_and_keyword_groups(self):
        status, _, body = get(self.base, "/api/data")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["thresholds"], POLICIES["escalation"]["presets"])
        self.assertIn("Trade", data["keywordGroups"])
        self.assertEqual(data["keywordCoverage"]["enabledKeywords"], 11)
        self.assertEqual(data["keywordCoverage"]["configuredVariants"], 58)
        self.assertEqual(len(data["calls"]) + len(data["pending"]), 42)
        if data["calls"]:
            item = data["calls"][0]["evidence"][0]
            self.assertTrue({"segment_ids", "evidence_start", "evidence_end", "clip_start", "clip_end"} <= set(item))

    def test_keyword_save_persists_only_a_valid_configuration(self):
        _, _, before_body = get(self.base, "/api/data")
        before = {call["id"]: call["classificationByThreshold"] for call in json.loads(before_body)["calls"]}
        edited = ex.load_json(self.keyword_path)
        for keyword in edited["keywords"]:
            keyword["enabled"] = False
        status, _, body = post(self.base, "/api/keywords", edited)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["keywords"]["keywords"][0]["enabled"])
        self.assertFalse(ex.load_json(self.keyword_path)["keywords"][0]["enabled"])
        _, _, after_body = get(self.base, "/api/data")
        after = json.loads(after_body)
        self.assertEqual({call["id"]: call["classificationByThreshold"] for call in after["calls"]}, before)
        self.assertEqual((after["keywordCoverage"]["enabledKeywords"], after["keywordCoverage"]["occurrences"]), (0, 0))

        invalid = json.loads(json.dumps(edited))
        invalid["keywords"][1]["de"].append(invalid["keywords"][0]["de"][0])
        status, _, body = post(self.base, "/api/keywords", invalid)
        self.assertEqual(status, 400)
        self.assertIn("duplicate term", json.loads(body)["error"])
        self.assertFalse(ex.load_json(self.keyword_path)["keywords"][0]["enabled"])

    def test_policy_save_is_validated_and_atomic(self):
        edited = ex.load_json(self.policies_path)
        edited["families"]["new_identifier"] = {"title": "New identifier policy", "enabled": True,
                                                   "can_alarm": False, "conditions": {}, "role_requirements": {}}
        status, _, body = post(self.base, "/api/policies", edited)
        self.assertEqual(status, 200)
        self.assertIn("new_identifier", json.loads(body)["policies"]["families"])
        self.assertIn("new_identifier", ex.load_json(self.policies_path)["families"])

        invalid = ex.load_json(self.policies_path)
        invalid["families"]["unsafe"] = {"title": "Unsafe", "enabled": True, "can_alarm": True, "conditions": {}}
        status, _, body = post(self.base, "/api/policies", invalid)
        self.assertEqual(status, 400)
        self.assertIn("needs at least one condition", json.loads(body)["error"])
        self.assertNotIn("unsafe", ex.load_json(self.policies_path)["families"])

    def test_transcript_correction_is_saved_as_an_overlay(self):
        source = ex.load_json(ROOT / "data" / "Transcriptions" / "Stufe1_D02-K1.json")["segments"][0]
        status, _, body = post(self.base, "/api/transcripts/Stufe1_D02-K1",
                               {"segment_id": source["id"], "text": "Korrigierter menschlicher Text."})
        saved = json.loads(body)
        self.assertEqual((status, saved["transcript"]["segment"]["text"]), (200, "Korrigierter menschlicher Text."))
        corrected, changed = ui.human_feedback.apply_corrections("Stufe1_D02-K1", [source], self.corrections_dir)
        self.assertEqual((corrected[0]["text"], changed), ("Korrigierter menschlicher Text.", [source["id"]]))



if __name__ == "__main__":
    unittest.main()
