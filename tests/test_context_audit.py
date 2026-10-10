"""Offline regressions for context-sensitive decisions and audio/source separation."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from callguard import asr  # noqa: E402
from callguard import decide as dec  # noqa: E402
from callguard import extract as ex  # noqa: E402
from callguard import pipeline  # noqa: E402


POLICIES = ex.load_json(ROOT / "config" / "policies.json")
KEYWORDS = ex.load_json(ROOT / "data" / "Stichwortliste.json")
SEGMENTS = [
    {"id": "context", "start": 0.0, "end": 1.0, "text": "The decisive context is stated here."},
    {"id": "action", "start": 1.0, "end": 2.0, "text": "The later requested action is stated here."},
]


def ref(segment="context"):
    return {"segment_id": segment, "quote": "The decisive context is stated here."}


def condition(state):
    return {"state": state, "evidence": [] if state == "unknown" else [ref()]}


def extraction(family, states=None, object_type=None, actor=None):
    """One complete, synthetic extraction; no dataset identifiers or gold labels enter it."""
    families = {name: {"events": []} for name in POLICIES["families"]}
    if object_type is not None:
        event = {"object_type": object_type, "evidence": [ref()]}
    else:
        event = {"conditions": {name: condition(state) for name, state in states.items()}}
    if actor is not None:
        event["actor"] = actor
    families[family]["events"] = [event]
    return {"families": families}


class ContextPairRegressionTest(unittest.TestCase):
    def test_changing_only_the_context_dependent_fact_has_the_expected_effect(self):
        """The label changes only for pairs whose policy fact is alarm-generating."""
        cases = (
            # A number classification is contextual, but numbers are classification-only by policy.
            ("number origin", "numbers", None, "full_payment_card_number", "invoice_reference",
             "no_alert", "no_alert"),
            ("masked-number origin", "numbers", None, "masked_payment_card_digits", "reservation_reference",
             "no_alert", "no_alert"),
            ("public versus non-public source", "trade", "information_nonpublic", "false", "true",
             "no_alert", "alarm"),
            ("expired help sample versus active secret", "access", "secret_active", "false", "true",
             "no_alert", "alarm"),
            ("authority confirmed versus absent", "disclosure", "authority_absent", "false", "true",
             "no_alert", "alarm"),
            ("independent invoices versus one evasion-motivated payment", "splitting", "movements_connected", "false", "true",
             "no_alert", "alarm"),
        )
        for title, family, predicate, absent, present, absent_label, present_label in cases:
            with self.subTest(title=title):
                if family == "numbers":
                    without = extraction(family, object_type=absent)
                    with_context = extraction(family, object_type=present)
                else:
                    all_true = {name: "true" for name in POLICIES["families"][family]["conditions"]}
                    without_states = {**all_true, predicate: absent}
                    with_states = {**without_states, predicate: present}  # the sole mutated fact
                    without = extraction(family, states=without_states)
                    with_context = extraction(family, states=with_states)

                no_context = dec.decide(without, SEGMENTS, POLICIES)
                decisive_context = dec.decide(with_context, SEGMENTS, POLICIES)
                self.assertEqual(no_context["label"], absent_label)
                self.assertEqual(decisive_context["label"], present_label)
                if predicate:
                    condition_result = decisive_context["events"][0]["conditions"][predicate]
                    self.assertEqual(condition_result["state"], present)
                    self.assertEqual(condition_result["evidence"][0]["segment_ids"], ["context"])
                else:
                    self.assertEqual(decisive_context["events"][0]["object_type"], present)

    def test_removed_context_becomes_unknown_not_the_opposite_fact(self):
        states = {"value_spoken": "true", "access_secret": "true", "secret_active": "false"}
        explicitly_expired = dec.decide(extraction("access", states=states), SEGMENTS, POLICIES)
        missing = copy.deepcopy(states)
        missing["secret_active"] = "unknown"
        undetermined = dec.decide(extraction("access", states=missing), SEGMENTS, POLICIES)
        self.assertEqual(explicitly_expired["label"], "no_alert")
        self.assertEqual((undetermined["label"], undetermined["reasons"]),
                         ("review", ["missing_policy_fact"]))
        self.assertEqual(undetermined["events"][0]["conditions"]["secret_active"]["state"], "unknown")

    def test_full_transcript_is_passed_even_when_highlights_exist(self):
        segments = ex.segments_from_transcript(ROOT / "data" / "Transkript" / "Stufe2_C16.txt")
        messages = ex.build_messages(segments, POLICIES, view=None)
        model_input = messages[-1]["content"]
        # The early context and later request are both present in the real full-text prompt.
        self.assertIn(segments[3]["text"], model_input)
        self.assertIn(segments[9]["text"], model_input)
        with mock.patch.object(ex, "extract", return_value=(extraction("numbers", object_type="other"), False)) as call:
            pipeline.run_segments("opaque-call", "fixture", segments, POLICIES, KEYWORDS, None, None,
                                  speakers=True, audio=False)
        self.assertIsNone(call.call_args.kwargs["view"])


class ReviewReasonRegressionTest(unittest.TestCase):
    def test_missing_policy_fact_is_semantic_review_with_the_matching_question(self):
        states = {"value_spoken": "true", "access_secret": "true", "secret_active": "unknown"}
        result = dec.decide(extraction("access", states=states), SEGMENTS, POLICIES)
        event = result["events"][0]
        self.assertEqual((result["label"], result["reasons"], event["reason"]),
                         ("review", ["missing_policy_fact"], "missing_policy_fact"))
        self.assertEqual(event["conditions"]["secret_active"]["state"], "unknown")
        self.assertEqual(event["conditions"]["secret_active"]["grounding_issues"], [])
        self.assertIn(POLICIES["families"]["access"]["conditions"]["secret_active"], event["open_questions"])

    def test_actor_role_unknown_is_semantic_review_not_a_provider_failure(self):
        states = {name: "true" for name in POLICIES["families"]["disclosure"]["conditions"]}
        result = dec.decide(extraction("disclosure", states=states,
                                       actor={"role": "unknown", "status": "unknown", "segment_ids": []}),
                            SEGMENTS, POLICIES)
        event = result["events"][0]
        self.assertEqual((result["label"], result["reasons"], event["reason"]),
                         ("review", ["actor_role_unknown"], "actor_role_unknown"))
        self.assertEqual(event["conditions"]["advisor_disclosed"]["state"], "unknown")
        self.assertIn("Was the person disclosing", event["open_questions"][-1])

    def test_bad_grounding_is_technical_review_not_missing_fact_review(self):
        states = {"value_spoken": "true", "access_secret": "true", "secret_active": "true"}
        broken = extraction("access", states=states)
        broken["families"]["access"]["events"][0]["conditions"]["secret_active"]["evidence"] = [
            {"segment_id": "context", "quote": "words never said by the transcript"}
        ]
        result = dec.decide(broken, SEGMENTS, POLICIES)
        condition_result = result["events"][0]["conditions"]["secret_active"]
        self.assertEqual((result["label"], result["reasons"]), ("review", ["technical_uncertainty"]))
        self.assertEqual(condition_result["state"], "unknown")
        self.assertTrue(condition_result["grounding_issues"])


class AudioIsolationRegressionTest(unittest.TestCase):
    def test_audio_path_uses_asr_segments_without_reading_gold_or_scripts(self):
        transcript = {"segments": [{"id": "s001", "start": 0.0, "end": 1.0, "text": "ASR text only."}]}
        captured = {}

        def fake_extract(segments, *_args, **kwargs):
            captured["segments"] = segments
            captured["view"] = kwargs.get("view")
            return extraction("numbers", object_type="other"), False

        with mock.patch.object(asr, "transcribe", return_value=(transcript, False)), \
                mock.patch.object(ex, "extract", side_effect=fake_extract), \
                mock.patch.object(pipeline, "gold", side_effect=AssertionError("gold must not enter audio inference")):
            result, _ = pipeline.run_audio(Path("opaque-recording.wav"), POLICIES, KEYWORDS)

        self.assertEqual(result["label"], "no_alert")
        self.assertEqual(captured["segments"][0]["text"], "ASR text only.")
        self.assertIsNone(captured["view"])


if __name__ == "__main__":
    unittest.main()
