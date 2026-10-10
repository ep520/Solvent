import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from callguard import extract as ex
from callguard import models
from callguard import pipeline


def row(call, gold, pred, **more):
    return {"call": call, "gold": gold, "pred": pred, "reasons": more.pop("reasons", []),
            "technical_failure": more.pop("technical_failure", False), "asr_failure": more.pop("asr_failure", False),
            "extraction_error": more.pop("extraction_error", False), "incomplete_extraction": more.pop("incomplete_extraction", False),
            "grounding_issue": more.pop("grounding_issue", False), "asr_cache_hit": more.pop("asr_cache_hit", None),
            "extract_cache_hit": more.pop("extract_cache_hit", None), **more}


class EvaluationMetricsTest(unittest.TestCase):
    def test_three_class_matrix_and_alarm_to_review_are_not_true_positives(self):
        data = pipeline.metrics([
            row("Stufe1_D02-K1", "alarm", "alarm"),
            row("Stufe1_D04-K1", "alarm", "review"),
            row("Stufe2_C01-K3", "no_alert", "no_alert"),
            row("Stufe3_C17-K3", "review", "review"),
        ])
        c = data["classification"]
        self.assertEqual(c["confusion_gold_rows_prediction_columns"]["alarm"], {"alarm": 1, "review": 1, "no_alert": 0})
        self.assertEqual(c["alarm_recall"], {"numerator": 1, "denominator": 2, "value": .5})
        self.assertEqual(c["alarm_to_review"], {"numerator": 1, "denominator": 2, "value": .5})
        self.assertEqual(c["alarm_triage_coverage_alarm_or_review"], {"numerator": 2, "denominator": 2, "value": 1.0})

    def test_null_denominators_are_na_not_zero(self):
        data = pipeline.metrics([row("Stufe2_C01-K3", "no_alert", "no_alert")])["classification"]
        self.assertIsNone(data["alarm_precision"]["value"])
        self.assertIsNone(data["alarm_recall"]["value"])
        self.assertEqual(data["alarm_precision"]["denominator"], 0)

    def test_unresolved_stays_in_headline_denominator(self):
        data = pipeline.metrics([
            row("Stufe1_D02-K1", "alarm", "alarm"),
            row("Stufe1_D02-K2", "alarm", None, technical_failure=True, asr_failure=True, reasons=["asr_failure"]),
        ])
        self.assertEqual(data["counts"], {"expected": 2, "processed": 2, "with_prediction": 1, "unresolved": 1, "technical_failure": 1})
        self.assertEqual(data["classification"]["accuracy_headline_including_unresolved"], {"numerator": 1, "denominator": 2, "value": .5})

    def test_technical_fallback_remains_review_and_is_also_counted_as_failure(self):
        data = pipeline.metrics([row("Stufe2_C06-K3", "alarm", "review", technical_failure=True,
                                     extraction_error=True, grounding_issue=True, reasons=["technical_uncertainty"])])
        self.assertEqual(data["classification"]["confusion_gold_rows_prediction_columns"]["alarm"]["review"], 1)
        self.assertEqual(data["classification"]["technical_review_count"], 1)
        self.assertEqual(data["operations"]["extraction_or_schema_failures"], 1)
        self.assertEqual(data["operations"]["grounding_issues"], 1)

    def test_clean_noisy_are_grouped_by_dialogue(self):
        data = pipeline.paired_audio_metrics([
            row("Stufe1_D02-K1", "alarm", "alarm"), row("Stufe1_D02-K2", "alarm", "review"),
            row("Stufe1_D03-K1", "no_alert", "no_alert"), row("Stufe1_D03-K2", "no_alert", "no_alert"),
        ])
        self.assertEqual((data["unique_dialogues"], data["complete_pairs"], data["pairs_prediction_discordant"], data["dialogues_both_variants_correct"]), (2, 2, 1, 1))


class DatasetAndManifestTest(unittest.TestCase):
    def test_contract_flags_duplicate_and_missing_gold(self):
        known = pipeline.DATA / "Audio" / "Stufe1_D02-K1.wav"
        _, issues = pipeline.dataset_contract([known, known])
        self.assertTrue(any("duplicate recording_id" in issue for issue in issues))
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "Stufe9_Z99-K1.wav"
            missing.write_bytes(b"not evaluated")
            _, issues = pipeline.dataset_contract([missing])
        self.assertTrue(any("no gold script" in issue for issue in issues))

    def test_manifest_identifies_configuration_and_hashes(self):
        policies = ex.load_json(pipeline.ROOT / "config" / "policies.json")
        keywords = ex.load_json(pipeline.DATA / "Stichwortliste.json")
        args = SimpleNamespace(audio=False, no_speakers=False, policies=pipeline.ROOT / "config" / "policies.json",
                               keywords=pipeline.DATA / "Stichwortliste.json")
        manifest = pipeline.run_manifest(args, policies, keywords, models.resolve("chat"))
        self.assertEqual(manifest["chat"]["prompt_version"], ex.PROMPT_VERSION)
        self.assertEqual(manifest["policies"]["default_preset"], "balanced")
        self.assertIn("callguard/extract.py", manifest["code"]["source_sha256"])


class CanonicalResultEnvelopeTest(unittest.TestCase):
    def test_release_envelope_traces_rules_recording_review_reason_and_clip_bounds(self):
        policies = ex.load_json(pipeline.ROOT / "config" / "policies.json")
        result = {
            "call": "Stufe3_C17-K3", "source": "data/Audio/Stufe3_C17-K3.wav", "label": "review",
            "status": "ok", "reasons": ["missing_policy_fact"], "policies_version": "policies-0.5",
            "events": [{"family": "access", "title": "Disclosure of an active access secret", "label": "review",
                        "summary": "Not established: secret_active.", "reason": "missing_policy_fact",
                        "open_questions": ["Was the value valid for an active login?"], "issues": [],
                        "conditions": {"value_spoken": {"state": "true", "downgraded": False, "uncertainty": [], "grounding_issues": [],
                                                          "evidence": [{"segment_ids": ["s001"], "quote": "123456", "start": 5.0, "end": 7.0, "quality": None}]},
                                       "secret_active": {"state": "unknown", "downgraded": False, "uncertainty": [], "grounding_issues": [], "evidence": []}},
                        "evidence": []}], "issues": [], "disabled_families": []}
        original = (result["label"], list(result["reasons"]))
        pipeline.augment_canonical_result(result, policies, "eval-test-audio",
                                          recording={"recording_id": "Stufe3_C17-K3", "audio_hash": "a" * 64,
                                                     "audio_path": "data/Audio/Stufe3_C17-K3.wav"}, recording_duration_s=20.0)
        self.assertTrue(pipeline.validate_canonical_result(result, release=True))
        self.assertEqual((result["label"], result["reasons"]), original)
        self.assertEqual(result["schema_version"], pipeline.CANONICAL_RESULT_SCHEMA_VERSION)
        self.assertEqual(result["policy_version"], "policies-0.5")
        self.assertEqual(result["recording"]["sha256"], "a" * 64)
        self.assertIn("policy.access.secret_active", {rule["rule_id"] for rule in result["evaluated_rules"]})
        self.assertEqual(result["uncertainty"]["missing_call_facts"], ["Was the value valid for an active login?"])
        self.assertEqual(result["rationale"]["template"], "review_missing_required_fact")
        self.assertEqual(result["rationale"]["reason_codes"], ["missing_policy_fact"])
        self.assertEqual(result["rationale"]["conditions"],
                         [{"rule_id": "policy.access.secret_active", "state": "unknown"}])
        self.assertEqual(result["rationale"]["evidence"], [])
        ref = result["events"][0]["conditions"]["value_spoken"]["evidence"][0]
        self.assertEqual((ref["clip_start"], ref["clip_end"]), (0.0, 17.0))
        self.assertIn("not a fraud probability", result["asr_quality"]["interpretation"])

    def test_legacy_result_without_optional_release_envelope_stays_readable(self):
        legacy = {"label": "no_alert", "events": [], "status": "ok"}
        self.assertTrue(pipeline.validate_canonical_result(legacy))

    def test_rationale_templates_reference_existing_facts_not_new_model_text(self):
        policies = ex.load_json(pipeline.ROOT / "config" / "policies.json")
        evidence = {"segment_ids": ["s001"], "quote": "buy it", "start": 2.0, "end": 3.0}
        alarm = {"label": "alarm", "reasons": [], "events": [
            {"family": "trade", "label": "alarm", "conditions": {
                "own_trade_request": {"state": "true", "evidence": [evidence]},
                "information_nonpublic": {"state": "true", "evidence": [evidence]},
            }}]}
        no_alert = {"label": "no_alert", "reasons": [], "events": [
            {"family": "access", "label": "no_alert", "conditions": {
                "secret_active": {"state": "false", "evidence": [evidence]},
            }}]}
        no_candidate = {"label": "no_alert", "reasons": [], "events": []}
        technical = {"label": "review", "reasons": ["technical_uncertainty"], "events": []}

        alarm_rationale = pipeline._rationale(alarm, policies)
        self.assertEqual(alarm_rationale["template"], "alarm_supported_conditions")
        self.assertEqual({item["rule_id"] for item in alarm_rationale["conditions"]},
                         {"policy.trade.own_trade_request", "policy.trade.information_nonpublic"})
        self.assertEqual(alarm_rationale["evidence"][0]["segment_ids"], ["s001"])
        self.assertEqual(alarm_rationale["evidence"][0]["role"], "supports_condition")
        self.assertEqual(pipeline._rationale(no_alert, policies)["template"],
                         "no_alert_required_condition_excluded")
        self.assertEqual(pipeline._rationale(no_candidate, policies)["template"],
                         "no_alert_no_candidate_event")
        self.assertEqual(pipeline._rationale(technical, policies)["template"],
                         "review_technical_blocker")
