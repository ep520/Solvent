import json
import sys
import threading
import unittest
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
        return e.code, dict(e.headers), e.read()


def trade_extraction():
    c = lambda sid, q: {"state": "true", "evidence": [{"segment_id": sid, "quote": q}]}
    fams = {name: {"events": []} for name in POLICIES["families"]}
    fams["trade"]["events"].append({"conditions": {
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
        self.assertEqual((call["family"], call["date"], call["time"], call["confidence"]), ("Trade", "Level 1", "noisy audio", 67))
        self.assertEqual([e["segments"] for e in call["evidence"]], [["s001"], ["s002"]])
        self.assertEqual({c["state"] for c in call["conditions"]}, {"Supported"})
        self.assertEqual(len(call["transcript"]), 2)

    def test_call_without_events_has_a_placeholder_evidence(self):
        empty = {"families": {name: {"events": []} for name in POLICIES["families"]}}
        results = {"balanced": dec.decide(empty, SEGMENTS, POLICIES, threshold=0.6)}
        call = ui.dashboard_call("Stufe2_C01-K3", SEGMENTS, results, "balanced", 160)
        self.assertEqual((call["classificationByThreshold"]["balanced"], call["family"]), ("No alert", "None"))
        self.assertEqual(call["evidence"][0]["segments"], [])


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.srv = ui.make_server("127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

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

    def test_paths_outside_the_dashboard_and_audio_are_refused(self):
        for path in ("/../config/policies.json", "/audio/..%2Fconfig.wav", "/audio/missing.wav", "/clip/Stufe1_D02-K1.wav"):
            self.assertIn(get(self.base, path)[0], (400, 404), path)

    def test_api_returns_thresholds_and_keyword_groups(self):
        status, _, body = get(self.base, "/api/data")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["thresholds"], POLICIES["escalation"]["presets"])
        self.assertIn("Trade", data["keywordGroups"])
        self.assertEqual(len(data["calls"]) + len(data["pending"]), 42)


if __name__ == "__main__":
    unittest.main()
