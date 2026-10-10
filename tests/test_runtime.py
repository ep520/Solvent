"""Runtime adapters and the hidden-test protocol: in-process Whisper, predict/score. No GPU needed."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class FasterWhisperTest(unittest.TestCase):
    def test_in_process_whisper_returns_scored_segments(self):
        import types
        from callguard import models

        class Seg:
            def __init__(self, start, end, text):
                self.start, self.end, self.text = start, end, text
                self.avg_logprob, self.no_speech_prob, self.compression_ratio = -0.2, 0.01, 1.3

        calls = {}

        class WhisperModel:
            def __init__(self, name, device, compute_type):
                calls["init"] = (name, device)

            def transcribe(self, path, **kwargs):
                calls["kwargs"] = kwargs
                return iter([Seg(0.0, 2.5, " Grüezi mitenand."), Seg(2.5, 5.0, " Ich wett chaufe.")]), None

        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = WhisperModel
        config = models.load_config()
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}), mock.patch.dict(models._fw_models, clear=True):
            p = models.check("asr", "faster_whisper", config, require_self_hostable=True)
            segs = models.transcribe("call.wav", profile="faster_whisper", config=config)
        self.assertEqual(p["model"], "large-v3")
        self.assertEqual([s["text"] for s in segs], ["Grüezi mitenand.", "Ich wett chaufe."])
        self.assertEqual((segs[1]["id"], segs[1]["avg_logprob"]), ("s002", -0.2))
        self.assertEqual(calls["kwargs"]["language"], "de")
        self.assertIsNone(calls["kwargs"]["initial_prompt"])   # no English instruction text as Whisper context


class AnswerFormatTest(unittest.TestCase):
    M_FILE = ("M01 · Rückruf zum Depot\nAufgabe: Vertrauliche Information und Kaufwunsch\nBearbeitung: alarm\n"
              "Ergebnis: Handelsereignis Kundschaft\nBegründung: ...\nBelegstellen laut Skript:\n"
              "  T008 K: «Ich möcht en einzelne Kauf.» (...)\n  T018 K: «... no nöd publiziert.» (...)\n\n"
              "Hinweis: von zwei Personen eingesprochen.\n\nT001 B: Grüezi\n")

    def test_m_file_answers_are_read(self):
        from callguard import pipeline
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Stufe2_M01.txt").write_text(self.M_FILE, encoding="utf-8")
            g = pipeline.gold("Stufe2_M01", tmp)
        self.assertEqual((g["label"], g["family"], g["event"], g["evidence"]),
                         ("alarm", None, "Handelsereignis Kundschaft", ["T008", "T018"]))

    def test_c_file_answers_still_read(self):
        from callguard import pipeline
        text = "Testziel: trade\nEreignis: absent\nBearbeitung: no_alert\nPrüfpräfix bis T032; Belege: T006, T032\n"
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Stufe2_C05.txt").write_text(text, encoding="utf-8")
            g = pipeline.gold("Stufe2_C05", tmp)
        self.assertEqual((g["label"], g["family"], g["evidence"]), ("no_alert", "trade", ["T006", "T032"]))

    def test_human_recordings_need_no_clean_noisy_pair(self):
        from callguard import pipeline
        self.assertEqual(pipeline.recording_variant("Stufe2_M01"), "human")
        self.assertEqual(pipeline.recording_variant("Stufe1_D02-K2"), "noisy")
        with tempfile.TemporaryDirectory() as tmp:
            audio, gold_dir = Path(tmp, "Audio"), Path(tmp, "Skript_mit_Sollbewertung")
            audio.mkdir(); gold_dir.mkdir()
            wav = audio / "Stufe2_M01.wav"
            wav.write_bytes(b"RIFF0000")
            (gold_dir / "Stufe2_M01.txt").write_text(self.M_FILE, encoding="utf-8")
            with mock.patch.object(pipeline, "DATA", Path(tmp)), mock.patch.object(pipeline, "ROOT", Path(tmp)):
                records, issues = pipeline.dataset_contract([wav])
        self.assertEqual(issues, [])
        self.assertEqual((records[0]["variant"], records[0]["gold_label"]), ("human", "alarm"))


class HiddenProtocolTest(unittest.TestCase):
    def test_predict_refuses_threshold_override_and_score_detects_tampering(self):
        from callguard import pipeline
        with self.assertRaises(SystemExit) as stop:
            pipeline.main(["predict", "nowhere", "--threshold", "0.5"])
        self.assertIn("frozen default preset", str(stop.exception))
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "rows.json").write_text("[]", encoding="utf-8")
            (run / "SEALED.json").write_text(json.dumps({"sha256": {"rows.json": pipeline._sha256_text(run / "rows.json")}}))
            (run / "rows.json").write_text('[{"changed": true}]', encoding="utf-8")
            with self.assertRaises(SystemExit) as stop:
                pipeline.main(["score", str(run)])
        self.assertIn("modified after sealing", str(stop.exception))


if __name__ == "__main__":
    unittest.main()
