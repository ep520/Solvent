"""Counterfactuals (XAI), Qwen3 support in the model router, and the watched-folder ingest. No data folder needed."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from callguard import counterfactual as cf  # noqa: E402
from callguard import decide as dec  # noqa: E402
from callguard import extract as ex  # noqa: E402
from callguard import ingest  # noqa: E402
from callguard import models  # noqa: E402

POLICIES = ex.load_json(ROOT / "config" / "policies.json")
SEGMENTS = [
    {"id": "s1", "speaker": None, "start": 0.0, "end": 4.0, "text": "Mini Schwöschter het gseit, d Quartalszahle sind no nöd publiziert."},
    {"id": "s2", "speaker": None, "start": 4.0, "end": 8.0, "text": "Grad drum möcht ich d Aktie jetzt chaufe,", "avg_logprob": -0.1},
    {"id": "s3", "speaker": None, "start": 8.0, "end": 12.0, "text": "vor de Meldig.", "avg_logprob": -0.7},
    {"id": "s4", "speaker": None, "start": 12.0, "end": 16.0, "text": "Ich weiss nöd, öb ich das dörf säge.", "avg_logprob": -0.1},
]


def cond(state, *refs):
    return {"state": state, "evidence": [{"segment_id": s, "quote": q} for s, q in refs]}


def run(threshold=None, **overrides):
    conds = {
        "own_trade_request": cond("true", ("s2", "möcht ich d Aktie jetzt chaufe")),
        "information_nonpublic": cond("true", ("s1", "no nöd publiziert")),
        "information_market_relevant": cond("true", ("s1", "Quartalszahle")),
        "request_based_on_information": cond("true", ("s2", "Grad drum")),
    }
    fams = {name: {"events": []} for name in POLICIES["families"]}
    fams["trade"]["events"].append({"conditions": {**conds, **overrides}})
    return dec.decide({"families": fams}, SEGMENTS, POLICIES, threshold=threshold)


class CounterfactualTest(unittest.TestCase):
    def test_rule_matches_decide(self):
        self.assertEqual(cf.label_for(["true", "true"]), "alarm")
        self.assertEqual(cf.label_for(["true", "unknown"]), "review")
        self.assertEqual(cf.label_for(["unknown", "false"]), "no_alert")
        self.assertEqual(cf.label_for(["true"], low_quality=True), "review")

    def test_alarm_lists_each_exclusion(self):
        e = run()["events"][0]
        self.assertEqual(e["label"], "alarm")
        to_no_alert = {c["condition"] for c in e["counterfactuals"] if c["label"] == "no_alert"}
        self.assertEqual(to_no_alert, set(POLICIES["families"]["trade"]["conditions"]))
        self.assertTrue(all(c["label"] != "alarm" for c in e["counterfactuals"]))

    def test_review_names_the_fact_that_would_decide(self):
        e = run(information_nonpublic=cond("unknown"))["events"][0]
        self.assertEqual(e["label"], "review")
        flips = {(c["condition"], c["to"]): c["label"] for c in e["counterfactuals"]}
        self.assertEqual(flips[("information_nonpublic", "true")], "alarm")
        self.assertEqual(flips[("information_nonpublic", "false")], "no_alert")
        self.assertIn("information nonpublic", e["counterfactuals"][0]["text"] + e["counterfactuals"][1]["text"])

    def test_every_counterfactual_really_changes_the_label(self):
        for overrides in ({}, {"information_nonpublic": cond("unknown")},
                          {"own_trade_request": cond("false", ("s4", "Ich weiss nöd"))}):
            e = run(**overrides)["events"][0]
            for c in e["counterfactuals"]:
                if c["kind"] == "condition":
                    states = {n: x["state"] for n, x in e["conditions"].items()}
                    states[c["condition"]] = c["to"]
                    self.assertNotEqual(cf.label_for(states.values(), bool(e.get("low_quality")) and c["to"] != "false"),
                                        e["label"])
                    self.assertEqual(c["label"], cf.label_for(states.values(), bool(e.get("low_quality")) and c["to"] != "false"))

    def test_threshold_counterfactual(self):
        # One condition rests only on s3 (ASR quality exp(-0.7) ≈ 0.497): alarm at 0.4, review at every preset.
        d = run(threshold=0.4, request_based_on_information=cond("true", ("s3", "vor de Meldig")))
        e = d["events"][0]
        self.assertEqual(e["label"], "alarm")
        presets = {c["preset"]: c["label"] for c in e["counterfactuals"] if c["kind"] == "threshold"}
        self.assertEqual(presets, {"sensitive": "review", "balanced": "review", "conservative": "review"})

    def test_numbers_have_no_counterfactuals(self):
        fams = {name: {"events": []} for name in POLICIES["families"]}
        fams["numbers"]["events"].append({"object_type": "iban", "evidence": [{"segment_id": "s3", "quote": "vor de Meldig"}]})
        d = dec.decide({"families": fams}, SEGMENTS, POLICIES)
        self.assertEqual(d["events"][0]["counterfactuals"], [])

    def test_explain_prints_what_if(self):
        d = run(information_nonpublic=cond("unknown"))
        self.assertIn("WHAT-IF:", dec.explain(d, SEGMENTS))


class Qwen3RouterTest(unittest.TestCase):
    def test_think_block_with_braces_is_ignored(self):
        text = '<think>maybe {"state": "true"} ... no</think>\n{"families": {}}'
        self.assertEqual(models._parse_json_object(text), {"families": {}})
        self.assertEqual(models._parse_json_object('reasoning {x}</think>{"a": 1}'), {"a": 1})

    def test_model_name_from_env_and_extra_body(self):
        config = models.load_config()
        with mock.patch.dict(os.environ, {"QWEN_MODEL": "Qwen/Qwen3-8B-AWQ", "QWEN_URL": "http://h/v1", "QWEN_KEY": "k"}):
            p = models.check("chat", "qwen3", config, require_self_hostable=True)
            self.assertEqual(p["model"], "Qwen/Qwen3-8B-AWQ")
            sent = {}

            def fake_post(profile, path, body, ctype):
                sent.update(json.loads(body))
                return {"choices": [{"message": {"content": '<think>{}</think>{"ok": true}'}}]}

            with mock.patch.object(models, "_post", fake_post):
                out = models.chat_json([{"role": "user", "content": "x"}], {"type": "object"}, "qwen3_fast", config)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(sent["top_k"], 20)
        self.assertEqual(sent["temperature"], 0.7)

    def test_qwen_profiles_are_self_hostable(self):
        profiles = models.load_config()["profiles"]
        for name in ("qwen3", "qwen3_fast", "ollama_qwen3", "whisper_local"):
            self.assertTrue(profiles[name]["self_hostable"], name)


class IngestTest(unittest.TestCase):
    def test_inbox_file_is_processed_and_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inbox, audio, results = tmp / "Inbox", tmp / "Audio", tmp / "runs"
            inbox.mkdir()
            (inbox / "call1.wav").write_bytes(b"RIFF0000")
            seen = {}

            def fake_process(target, policies, keywords, args):
                seen["target"] = target
                return {"call": target.stem, "label": "no_alert", "reasons": [], "events": []}

            with mock.patch.multiple(ingest, INBOX=inbox, AUDIO=audio, RESULTS=results, FAILED=inbox / "failed",
                                     process=fake_process, stable=lambda p: True):
                self.assertEqual(ingest.scan(POLICIES, {}, mock.Mock()), 1)
            self.assertEqual(seen["target"], audio / "call1.wav")
            self.assertFalse((inbox / "call1.wav").exists())
            self.assertTrue((audio / "call1.wav").exists())

    def test_failure_is_quarantined_with_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inbox, audio = tmp / "Inbox", tmp / "Audio"
            inbox.mkdir()
            (inbox / "bad.wav").write_bytes(b"RIFF0000")

            def boom(*a):
                raise models.ModelError("ASR down")

            with mock.patch.multiple(ingest, INBOX=inbox, AUDIO=audio, FAILED=inbox / "failed",
                                     process=boom, stable=lambda p: True):
                self.assertEqual(ingest.scan(POLICIES, {}, mock.Mock()), 0)
            self.assertTrue((inbox / "failed" / "bad.wav").exists())
            self.assertIn("ASR down", (inbox / "failed" / "bad.err").read_text())
            self.assertFalse((audio / "bad.wav").exists())

    def test_trigger_payload(self):
        result = run()
        result["call"] = "call1"
        captured = {}

        class Resp:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout):
            captured.update(json.loads(req.data))
            return Resp()

        with mock.patch.object(ingest.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(ingest.report(result, "http://t/triggers", "http://localhost:8090"), 201)
        self.assertTrue(captured["title"].startswith("ALARM:"))
        self.assertTrue(captured["url"].startswith("http://localhost:8090/#call1"))


if __name__ == "__main__":
    unittest.main()
