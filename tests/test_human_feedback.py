import tempfile
import unittest
from pathlib import Path

from callguard import extract as ex
from callguard import human_feedback as feedback


SEGMENTS = [
    {"id": "s001", "text": "Das ist die automatische Transkription.", "start": 0, "end": 2},
    {"id": "s002", "text": "Bitte kaufen Sie die Aktie.", "start": 2, "end": 4},
]


class HumanFeedbackTest(unittest.TestCase):
    def test_correction_overlay_preserves_original_and_rejects_a_new_asr_text(self):
        with tempfile.TemporaryDirectory() as directory:
            corrections = Path(directory) / "corrections"
            feedback.save_correction("call-1", SEGMENTS, "s001", "Das ist die korrigierte Transkription.", corrections)
            corrected, changed = feedback.apply_corrections("call-1", SEGMENTS, corrections)
            self.assertEqual((corrected[0]["text"], changed), ("Das ist die korrigierte Transkription.", ["s001"]))
            changed_asr = [{**SEGMENTS[0], "text": "Neu transkribiert."}, SEGMENTS[1]]
            self.assertEqual(feedback.apply_corrections("call-1", changed_asr, corrections)[0][0]["text"], "Neu transkribiert.")

    def test_review_outcome_becomes_bounded_calibration_example_and_excludes_its_own_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviews.json"
            feedback.save_review("call-1", "alarm", "Trade", ["missing_policy_fact"], SEGMENTS, path)
            examples = feedback.calibration_examples(path)
            self.assertEqual(examples[0]["outcome"], "alarm")
            self.assertEqual(feedback.calibration_examples(path, exclude_call="call-1"), [])
            self.assertIn("Past human resolution: alert", feedback.prompt_examples(examples)[0])

    def test_feedback_changes_the_extraction_cache_key(self):
        policies = ex.load_json(ex.ROOT / "config" / "policies.json")
        profile = {"name": "test", "model": "test-model"}
        with tempfile.TemporaryDirectory() as directory:
            plain = ex.cache_path(SEGMENTS, policies, profile, directory, feedback_examples=[])
            examples = [{"call": "other", "outcome": "alarm", "family": "Trade", "review_reasons": [], "segments": SEGMENTS}]
            calibrated = ex.cache_path(SEGMENTS, policies, profile, directory, feedback_examples=examples)
        self.assertNotEqual(plain, calibrated)
