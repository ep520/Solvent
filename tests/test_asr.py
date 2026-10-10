import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from callguard import asr  # noqa: E402


class AsrTest(unittest.TestCase):
    def test_annotation_retains_order_and_does_not_guess_a_role(self):
        annotated = asr._annotate([{"id": "s001", "start": 0, "end": 1.5, "text": " Grüezi ", "avg_logprob": -0.2}])
        self.assertEqual(annotated[0]["turn"], "TURN_001")
        self.assertEqual(annotated[0]["role"], "UNKNOWN_ROLE")
        self.assertEqual(annotated[0]["speaker"], "UNKNOWN_SPEAKER")
        self.assertEqual(annotated[0]["text"], "Grüezi")

    def test_export_has_timestamps_and_english_metadata(self):
        result = {"source_file": "sample.wav", "segments": [{"id": "s001", "turn": "TURN_001", "role": "UNKNOWN_ROLE",
                  "speaker": "UNKNOWN_SPEAKER", "start": 1.0, "end": 2.5, "text": "Grüezi"}]}
        with tempfile.TemporaryDirectory() as tmp:
            json_path, md_path = asr.export(result, tmp)
            self.assertEqual(json.loads(json_path.read_text())["segments"][0]["turn"], "TURN_001")
            page = md_path.read_text()
            self.assertIn("00:00:01.000", page)
            self.assertIn("UNKNOWN_ROLE", page)
            self.assertIn("TURN_001", page)

    def test_bad_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-monotonic"):
            asr._annotate([{"start": 2, "end": 1, "text": "bad"}])


if __name__ == "__main__":
    unittest.main()
