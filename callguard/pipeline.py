"""CLI: run the text pipeline on transcripts and evaluate it against the expected assessments.

  python3 -m callguard.pipeline text data/Transkript/Stufe1_D02.txt [--no-speakers]
  python3 -m callguard.pipeline eval [--no-speakers] [--only C09 | --smoke] [--profile private]
  python3 -m callguard.pipeline audio data/Audio/Stufe1_D02-K1.wav
  python3 -m callguard.pipeline eval --audio [--only C09 | --smoke]
  python3 -m callguard.pipeline freeze [--no-speakers]   # cached extractions -> tests/fixtures/replay
"""
import argparse
import csv
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from callguard import asr
from callguard import decide as dec
from callguard import evidence
from callguard import extract as ex
from callguard import models

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LABELS = ("alarm", "review", "no_alert")
STATES = ("true", "false", "unknown")
CANONICAL_RESULT_SCHEMA_VERSION = "callguard.canonical-result.v1"
SMOKE = ("Stufe1_D02", "Stufe1_D03", "Stufe2_C09", "Stufe2_C10", "Stufe3_C17")


def script_name(call):
    """Stufe1_D02-K2 -> Stufe1_D02 (audio file to its dialogue); used only to look up the expected assessment."""
    return re.sub(r"-K\d$", "", call)


def audio_segments(wav, profile=None):
    """Whisper segments for a WAV (hash-cached by callguard.asr); Whisper has no speakers, so none are shown."""
    started = time.perf_counter()
    transcript, cached = asr.transcribe(wav, profile=profile)
    return ([{**s, "speaker": None} for s in transcript["segments"]],
            {"asr_elapsed_s": round(time.perf_counter() - started, 3), "asr_cache_hit": cached})


def run_audio(wav, policies, keywords, profile=None, threshold=None, **_):
    call = Path(wav).stem
    try:
        segments, timing = audio_segments(wav, profile=profile)
    except Exception as error:
        # No transcript means no decision is possible.  Do not silently call it no_alert or manufacture
        # a policy review: evaluation records this explicitly as unresolved ASR failure.
        return ({"call": call, "source": str(Path(wav)), "speakers": False, "audio": True,
                 "status": "failed", "label": None, "reasons": ["asr_failure"], "error": str(error),
                 "keyword_hits": [], "_evaluation": {"asr_failure": True, "asr_elapsed_s": None,
                                                        "asr_cache_hit": None, "extract_cache_hit": None,
                                                        "decision_elapsed_s": None, "total_elapsed_s": None}}, [])
    return run_segments(call, str(Path(wav)), segments, policies, keywords, profile, threshold,
                        speakers=False, audio=True, stage_timing=timing)


def run_text(path, policies, keywords, speakers=True, profile=None, threshold=None):
    segments = ex.segments_from_transcript(path, speakers=speakers)
    return run_segments(Path(path).stem, str(Path(path)), segments, policies, keywords, profile, threshold,
                        speakers=speakers, audio=False)


def run_segments(call, source, segments, policies, keywords, profile, threshold, speakers, audio, stage_timing=None):
    started = time.perf_counter()
    stage_timing = dict(stage_timing or {})
    try:
        extraction, extract_cached = ex.extract(segments, policies, profile=profile, return_cache_status=True)
        error = None
    except models.ModelError as e:
        extraction, error, extract_cached = {}, e, None
    decision_started = time.perf_counter()
    result = dec.decide(extraction, segments, policies, threshold=threshold, error=error)
    result.update(call=call, source=source, speakers=speakers, audio=audio,
                  keyword_hits=ex.keyword_hits(segments, keywords))
    stage_timing.update({"asr_failure": False, "extract_cache_hit": extract_cached,
                         # A value stored in an old cache records that old model call, not this run's latency.
                         "extract_elapsed_s": extraction.get("_meta", {}).get("elapsed_s") if extraction and not extract_cached else None,
                         "decision_elapsed_s": round(time.perf_counter() - decision_started, 6),
                         "total_elapsed_s": round(time.perf_counter() - started, 6)})
    if error is None:
        stage_timing["threshold_labels"] = {name: dec.decide(extraction, segments, policies, threshold=value)["label"]
                                             for name, value in policies["escalation"]["presets"].items()}
    result["_evaluation"] = stage_timing
    return result, segments


def cited_segments(result):
    refs = [r for e in result["events"] for c in e["conditions"].values() for r in c["evidence"]]
    refs += [r for e in result["events"] for r in e["evidence"]]
    return {sid for r in refs for sid in r["segment_ids"]}


def _evidence_refs(result):
    """Yield canonical evidence objects in a result, once each by object identity."""
    seen = set()
    for event in result.get("events", []):
        refs = [ref for condition in event.get("conditions", {}).values()
                for ref in condition.get("evidence", [])]
        refs += event.get("evidence", [])
        for ref in refs:
            if id(ref) not in seen:
                seen.add(id(ref))
                yield ref


def _relative_path_or_original(path):
    """Keep release JSON portable when its input happens to be outside the repository."""
    if path is None:
        return None
    path = Path(path)
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _review_uncertainty(result):
    """Classify uncertainty already produced by the deterministic decision, without re-deciding."""
    missing_facts, grounding, extraction = [], [], []
    for event in result.get("events", []):
        if event.get("reason") in ("missing_policy_fact", "actor_role_unknown"):
            missing_facts += event.get("open_questions", [])
        for condition in event.get("conditions", {}).values():
            grounding += condition.get("grounding_issues", [])
            if condition.get("downgraded"):
                extraction.append("condition downgraded because no valid grounded evidence was available")
    if result.get("error"):
        extraction.append(str(result["error"]))
    return {
        "missing_call_facts": list(dict.fromkeys(missing_facts)),
        "technical": {
            "extraction_or_schema": list(dict.fromkeys(extraction)),
            "grounding": list(dict.fromkeys(grounding)),
            "provider": [str(result["error"])] if result.get("error") else [],
        },
    }


def _evaluated_rules(result, policies):
    """Expose stable policy IDs for facts already evaluated in each canonical event.

    These are derived from the policy keys and canonical condition states.  They are
    not model output and do not change how an event is labelled.
    """
    evaluated, alarm_generating = [], []
    for event_index, event in enumerate(result.get("events", [])):
        family = event.get("family")
        family_policy = policies.get("families", {}).get(family, {})
        can_alarm = bool(family_policy.get("can_alarm", True))
        family_rule_id = f"policy.{family}"
        decision = {"rule_id": family_rule_id, "event_index": event_index,
                    "kind": "family_decision", "outcome": event.get("label"),
                    "alarm_capable": can_alarm,
                    "generates_alarm": bool(can_alarm and event.get("label") == "alarm")}
        evaluated.append(decision)
        if decision["generates_alarm"]:
            alarm_generating.append({"rule_id": family_rule_id, "event_index": event_index})
        for condition_name, condition in event.get("conditions", {}).items():
            evaluated.append({"rule_id": f"policy.{family}.{condition_name}", "event_index": event_index,
                              "kind": "condition", "outcome": condition.get("state"),
                              "alarm_role": "required_condition" if can_alarm else "classification_only"})
    return evaluated, alarm_generating


def _readable_explanation(result):
    """A deterministic, human-readable summary of the canonical event output."""
    events = sorted(result.get("events", []), key=lambda event: dec.LABEL_RANK.get(event.get("label"), -1), reverse=True)
    if result.get("error"):
        return "A technical problem prevented a complete extraction; the result remains review."
    if not events:
        return "No candidate event was available for a policy decision."
    event = events[0]
    return f"{event.get('title', event.get('family', 'Policy event'))}. {event.get('summary', '')}".strip()


def _decision_event(result, label=None):
    """Return the highest-priority event relevant to the final decision.

    A call can contain several candidate events.  The rationale must point at
    the event that determined the call label, rather than narrating every
    extracted fact or relying on their extraction order.
    """
    label = result.get("label") if label is None else label
    candidates = [(index, event) for index, event in enumerate(result.get("events", []))
                  if event.get("label") == label]
    return candidates[0] if candidates else (None, None)


def _rationale_condition_refs(event_index, event, states, evidence_role):
    """Point at facts already present in the canonical event; never copy them."""
    refs, evidence = [], []
    for name, condition in (event or {}).get("conditions", {}).items():
        if condition.get("state") not in states:
            continue
        refs.append({"rule_id": f"policy.{event.get('family')}.{name}", "state": condition.get("state")})
        for ref in condition.get("evidence", []):
            evidence.append({"event_index": event_index, "condition": name,
                             "segment_ids": ref.get("segment_ids", []),
                             "start": ref.get("start"), "end": ref.get("end"), "role": evidence_role})
    return refs, evidence


def _rationale(result, policies):
    """Make a short post-decision rationale from existing rule output only.

    This deliberately runs *after* ``decide``.  It does not change a rule,
    infer a fact, create evidence, or call an LLM.  The text is a short
    rendering; ``conditions`` and ``evidence`` are references to the actual
    grounded facts kept in ``events``.
    """
    label = result.get("label")
    reason_codes = list(dict.fromkeys(result.get("reasons", [])))
    event_index, event = _decision_event(result)
    base = {"text": None, "template": None, "reason_codes": reason_codes,
            "event_index": event_index, "conditions": [], "evidence": []}

    if label == "alarm" and event:
        conditions, refs = _rationale_condition_refs(event_index, event, {"true"}, "supports_condition")
        return {**base, "template": "alarm_supported_conditions",
                "text": "Alarm: all required conditions are supported by grounded evidence.",
                "conditions": conditions, "evidence": refs}

    if label == "no_alert":
        if not event:
            return {**base, "template": "no_alert_no_candidate_event",
                    "text": "No alert: no candidate event was detected for an enabled policy."}
        conditions, refs = _rationale_condition_refs(event_index, event, {"false"}, "excludes_condition")
        if conditions:
            return {**base, "template": "no_alert_required_condition_excluded",
                    "text": "No alert: a required condition is explicitly excluded by grounded evidence.",
                    "conditions": conditions, "evidence": refs}
        family_policy = policies.get("families", {}).get(event.get("family"), {})
        if not event.get("conditions") and not family_policy.get("can_alarm", True):
            return {**base, "template": "no_alert_classification_only",
                    "text": "No alert: the detected event is classification-only and cannot raise an alarm."}
        return {**base, "template": "no_alert_no_decisive_condition",
                "text": "No alert: no alarm-generating condition was established."}

    if label == "review":
        event_reason = event.get("reason") if event else None
        codes = list(dict.fromkeys(reason_codes + ([event_reason] if event_reason else [])))
        base["reason_codes"] = codes
        technical = (bool(result.get("error")) or "technical_uncertainty" in codes or
                     "below_escalation_threshold" in codes)
        if technical:
            conditions, refs = _rationale_condition_refs(event_index, event, {"unknown"}, "relevant_context")
            if "below_escalation_threshold" in codes:
                text, template = ("Review: grounded evidence is below the ASR-quality escalation threshold.",
                                  "review_asr_quality_threshold")
            else:
                text, template = ("Review: a technical problem prevents a reliable decision.",
                                  "review_technical_blocker")
            return {**base, "template": template, "text": text,
                    "conditions": conditions, "evidence": refs}
        conditions, refs = _rationale_condition_refs(event_index, event, {"unknown"}, "relevant_context")
        if "actor_role_unknown" in codes:
            text, template = ("Review: the required speaker role is not established.",
                              "review_missing_actor_role")
        else:
            text, template = ("Review: a required fact is not established in the call.",
                              "review_missing_required_fact")
        return {**base, "template": template, "text": text,
                "conditions": conditions, "evidence": refs}

    # An unresolved ASR failure has no three-class label, but a release result
    # still needs a truthful rationale instead of a fabricated no-alert.
    return {**base, "template": "unresolved_technical_failure",
            "text": "No decision: a technical failure prevented a final result."}


def augment_canonical_result(result, policies, run_id, recording=None, recording_duration_s=None):
    """Add release traceability to a canonical result without modifying its decision fields.

    New fields are deliberately derived after decision-making: old cached results
    remain readable, and this function neither calls a model nor changes a label.
    """
    recording = recording or {}
    result["schema_version"] = CANONICAL_RESULT_SCHEMA_VERSION
    # Keep the historical plural spelling as-is and add the requested singular alias.
    result["policy_version"] = result.get("policies_version", policies.get("version"))
    result["run_id"] = run_id
    result["recording"] = {
        "id": recording.get("recording_id", result.get("call")),
        "sha256": recording.get("audio_hash"),
        "path": recording.get("audio_path", _relative_path_or_original(result.get("source"))),
        "duration_s": recording_duration_s,
    }
    evaluated, alarm_generating = _evaluated_rules(result, policies)
    result["evaluated_rules"] = evaluated
    result["alarm_generating_rules"] = alarm_generating
    result["uncertainty"] = _review_uncertainty(result)
    result["explanation"] = {
        "text": _readable_explanation(result),
        "open_questions": result["uncertainty"]["missing_call_facts"],
        "review_reasons": list(result.get("reasons", [])),
    }
    result["rationale"] = _rationale(result, policies)
    result["asr_quality"] = {
        "interpretation": "uncalibrated ASR transcription-quality heuristic; not a fraud probability",
        "threshold": result.get("threshold"),
        "unavailable_is_null": True,
    }
    for ref in _evidence_refs(result):
        start, end = ref.get("start"), ref.get("end")
        if recording_duration_s is not None and isinstance(start, (int, float)) and isinstance(end, (int, float)):
            clip_start, clip_end = evidence.window(start, end, recording_duration_s)
            ref["clip_start"] = clip_start
            ref["clip_end"] = clip_end
    return result


def validate_canonical_result(result, release=False):
    """Validate the stable core; `release=True` additionally requires new traceability fields.

    The default accepts pre-schema cache artefacts, so readers can continue to
    consume historic results.  This is deliberately a small validator for data
    emitted by this repository, not a replacement for the LLM extraction schema.
    """
    if result.get("label") not in LABELS and result.get("label") is not None:
        raise ValueError("result label must be alarm, review, no_alert or null")
    if not isinstance(result.get("events", []), list):
        raise ValueError("result events must be a list")
    for event in result.get("events", []):
        if event.get("label") not in LABELS:
            raise ValueError("event label must be a supported classification")
        if not isinstance(event.get("conditions", {}), dict):
            raise ValueError("event conditions must be an object")
        for condition in event["conditions"].values():
            if condition.get("state") not in STATES:
                raise ValueError("condition state must be true, false or unknown")
            for ref in condition.get("evidence", []):
                _validate_evidence_ref(ref)
        for ref in event.get("evidence", []):
            _validate_evidence_ref(ref)
    if release:
        if result.get("schema_version") != CANONICAL_RESULT_SCHEMA_VERSION:
            raise ValueError("release result has an unsupported schema_version")
        if not isinstance(result.get("run_id"), str) or not result["run_id"]:
            raise ValueError("release result needs a run_id")
        if not isinstance(result.get("recording"), dict) or not result["recording"].get("id"):
            raise ValueError("release result needs recording traceability")
        if result.get("policy_version") is None:
            raise ValueError("release result needs a policy_version")
        rationale = result.get("rationale")
        required_rationale_fields = {"text", "template", "reason_codes", "event_index", "conditions", "evidence"}
        if not isinstance(rationale, dict) or not required_rationale_fields <= rationale.keys():
            raise ValueError("release result needs a complete deterministic rationale")
        if not isinstance(rationale["text"], str) or not rationale["text"]:
            raise ValueError("rationale text must be a non-empty template result")
        if not isinstance(rationale["template"], str) or not rationale["template"]:
            raise ValueError("rationale template must be named")
        if not all(isinstance(code, str) for code in rationale["reason_codes"]):
            raise ValueError("rationale reason_codes must be strings")
        if rationale["event_index"] is not None and (not isinstance(rationale["event_index"], int) or
                                                      not 0 <= rationale["event_index"] < len(result["events"])):
            raise ValueError("rationale event_index must refer to an event or be null")
        if not isinstance(rationale["conditions"], list) or not isinstance(rationale["evidence"], list):
            raise ValueError("rationale conditions and evidence must be lists")
        if any(not isinstance(ref, dict) or
               ref.get("role") not in {"supports_condition", "excludes_condition", "relevant_context"}
               for ref in rationale["evidence"]):
            raise ValueError("rationale evidence references must name their role")
    return True


def _validate_evidence_ref(ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("segment_ids"), list) or not ref["segment_ids"]:
        raise ValueError("evidence needs one or more segment_ids")
    if any(not isinstance(segment_id, str) or not segment_id for segment_id in ref["segment_ids"]):
        raise ValueError("evidence segment_ids must be non-empty strings")
    start, end = ref.get("start"), ref.get("end")
    if start is not None and end is not None and (not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or start < 0 or end < start):
        raise ValueError("evidence timestamps must be an ordered non-negative range")
    if "clip_start" in ref or "clip_end" in ref:
        clip_start, clip_end = ref.get("clip_start"), ref.get("clip_end")
        if not all(isinstance(value, (int, float)) for value in (clip_start, clip_end)) or clip_start < 0 or clip_end < clip_start:
            raise ValueError("evidence clip bounds must be an ordered non-negative range")
        if start is not None and (clip_start > start or clip_end < end):
            raise ValueError("evidence clip bounds must contain the cited timestamp range")


def gold(call):
    """Expected assessment from Skript_mit_Sollbewertung; used only for scoring, never as input."""
    text = (DATA / "Skript_mit_Sollbewertung" / f"{call}.txt").read_text(encoding="utf-8")
    field = lambda name: re.search(rf"^{name}: (.+)$", text, re.M).group(1).strip()
    return {"label": field("Bearbeitung"), "family": field("Testziel"), "event": field("Ereignis"),
            "evidence": re.findall(r"T\d{3}", field("Prüfpräfix bis T\\d{3}; Belege"))}


def recording_variant(recording_id):
    """Dataset convention only; never used by inference."""
    suffix = Path(recording_id).stem.rsplit("-", 1)[-1]
    return {"K1": "clean", "K3": "clean", "K2": "noisy", "K4": "noisy"}.get(suffix, "unknown")


def dataset_contract(audio_paths=None):
    """Build and validate the minimum release contract from supplied files and gold scripts.

    Gold lives in the supplied ``Skript_mit_Sollbewertung`` files.  This function is called only after
    inference, for scoring and release artefacts; it never supplies semantic content to extraction.
    """
    audio_paths = list(audio_paths or sorted((DATA / "Audio").glob("*.wav")))
    scripts = {p.stem: p for p in (DATA / "Skript_mit_Sollbewertung").glob("*.txt")}
    records, issues, seen = [], [], set()
    for audio_path in audio_paths:
        recording_id = audio_path.stem
        dialogue_id = script_name(recording_id)
        if recording_id in seen:
            issues.append(f"duplicate recording_id: {recording_id}")
            continue
        seen.add(recording_id)
        if dialogue_id not in scripts:
            issues.append(f"{recording_id}: no gold script for dialogue {dialogue_id}")
            continue
        try:
            expected = gold(dialogue_id)
        except (AttributeError, OSError) as error:
            issues.append(f"{recording_id}: unreadable gold: {error}")
            continue
        if expected["label"] not in LABELS:
            issues.append(f"{recording_id}: invalid gold label {expected['label']!r}")
            continue
        records.append({"recording_id": recording_id, "dialogue_id": dialogue_id,
                        "audio_path": str(audio_path.relative_to(ROOT)), "audio_hash": asr.sha256(audio_path),
                        "variant": recording_variant(recording_id), "split": "development",
                        "gold_label": expected["label"],
                        "gold_source": str(scripts[dialogue_id].relative_to(ROOT)),
                        # Present in the supplied gold file; do not infer meanings for its R/M identifiers.
                        "family": expected["family"], "gold_event": expected["event"],
                        "gold_evidence": expected["evidence"]})
    by_dialogue = {}
    for record in records:
        by_dialogue.setdefault(record["dialogue_id"], []).append(record)
    for dialogue_id, pair in by_dialogue.items():
        labels = {r["gold_label"] for r in pair}
        variants = [r["variant"] for r in pair]
        if len(labels) != 1:
            issues.append(f"{dialogue_id}: gold labels disagree across recordings")
        if len(pair) != 2 or sorted(variants) != ["clean", "noisy"]:
            issues.append(f"{dialogue_id}: expected one clean/noisy pair, found {variants}")
    return records, issues


def nullable_ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": round(numerator / denominator, 6) if denominator else None}


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 3)


def time_summary(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return {"n": len(values), "mean_s": round(statistics.mean(values), 3) if values else None,
            "median_s": round(statistics.median(values), 3) if values else None,
            "p95_s": percentile(values, .95)}


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_commit():
    """Best-effort commit hash for the manifest; absent outside a git checkout."""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip()
    except Exception:
        return None


def git_dirty():
    try:
        return bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                                   text=True, timeout=5, check=True).stdout.strip())
    except Exception:
        return None


def source_hashes():
    files = ("callguard/pipeline.py", "callguard/extract.py", "callguard/decide.py", "callguard/asr.py",
             "callguard/models.py", "config/models.json")
    return {name: file_sha256(ROOT / name) for name in files if (ROOT / name).is_file()}


def run_manifest(args, policies, keywords, chat_profile, asr_profile=None, contract=None, started_at=None,
                 finished_at=None, wall_clock_s=None, run_id=None):
    """What produced this run: model, prompt/schema/policy versions and hashes, thresholds, compliance flags.

    Freezing this alongside a run's results is what makes the run reproducible and auditable: anyone can
    check, after the fact, exactly which endpoint, prompt and configuration a given alarm came from."""
    return {
        "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_started_at": started_at,
        "run_finished_at": finished_at,
        "batch_wall_clock_s": wall_clock_s,
        "code": {"git_commit": git_commit(), "dirty": git_dirty(), "source_sha256": source_hashes()},
        "mode": "audio" if args.audio else "nospeakers" if args.no_speakers else "speakers",
        "chat": {"profile": chat_profile["name"], "model": chat_profile["model"],
                "self_hostable": bool(chat_profile.get("self_hostable")), "prompt_version": ex.PROMPT_VERSION,
                "api": chat_profile.get("api"), "generation": {"temperature": 0, "seed": chat_profile.get("seed", 7), **{k: chat_profile.get(k) for k in
                    ("max_output_tokens", "json_mode", "strict_schema", "timeout", "retries", "parse_retries") if k in chat_profile}}},
        "asr": ({"profile": asr_profile["name"], "model": asr_profile.get("asr_model"),
                "self_hostable": bool(asr_profile.get("self_hostable")), "api": asr_profile.get("api"),
                "generation": {k: asr_profile.get(k) for k in ("timeout", "retries", "retry_backoff") if k in asr_profile}}
                if asr_profile else None),
        "policies": {"version": policies.get("version"), "sha256": file_sha256(args.policies),
                    "escalation_presets": policies["escalation"]["presets"],
                    "default_preset": policies["escalation"]["default_preset"],
                    "grounding": policies.get("grounding", {}).get("quote_matching"),
                    "disabled_families": [name for name, fam in policies["families"].items()
                                          if not fam.get("enabled", True)]},
        "keywords": {"sha256": file_sha256(args.keywords)},
        "dataset": {"contract_records": len(contract or []),
                    "recording_hashes": {r["recording_id"]: r["audio_hash"] for r in (contract or [])},
                    "split": "development" if contract else None},
        "cache": {"asr": "content hash + ASR profile/model/constraint", "extraction": "profile/model/prompt/messages/schema",
                  "rule": "cached extraction is valid only when its cache key matches these inputs"},
        "self_hostable_compliant": bool(chat_profile.get("self_hostable")) and
                                   (asr_profile is None or bool(asr_profile.get("self_hostable"))),
    }


def metrics(rows):
    """Three-class and operational metrics, without treating an unresolved record as a no-alert."""
    confusion = {gold_label: {prediction: 0 for prediction in LABELS} for gold_label in LABELS}
    for row in rows:
        if row.get("pred") in LABELS:
            confusion[row["gold"]][row["pred"]] += 1
    total = len(rows)
    resolved = [r for r in rows if r.get("pred") in LABELS]
    unresolved = [r for r in rows if r.get("pred") not in LABELS]
    n_alarm = sum(r["gold"] == "alarm" for r in rows)
    n_not_alarm = total - n_alarm
    pred_alarm = sum(r.get("pred") == "alarm" for r in rows)
    true_alarm = confusion["alarm"]["alarm"]
    false_alarms = sum(r.get("pred") == "alarm" and r["gold"] != "alarm" for r in rows)
    correct = sum(r.get("pred") == r["gold"] for r in rows)
    review_reasons = {}
    for row in rows:
        if row.get("pred") == "review":
            for reason in row.get("reasons") or ["unspecified"]:
                review_reasons[reason] = review_reasons.get(reason, 0) + 1
    technical_review_reasons = {"technical_uncertainty", "asr_failure", "extraction_failure"}
    by_level = {}
    for level in ("Stufe1", "Stufe2", "Stufe3"):
        subset = [r for r in rows if r["call"].startswith(level)]
        by_level[level] = {"correct": sum(r.get("pred") == r["gold"] for r in subset), "total": len(subset),
                           "accuracy": nullable_ratio(sum(r.get("pred") == r["gold"] for r in subset), len(subset))}
    answer = {
        "counts": {"expected": total, "processed": total, "with_prediction": len(resolved),
                   "unresolved": len(unresolved), "technical_failure": sum(bool(r.get("technical_failure")) for r in rows)},
        "classification": {
            "confusion_gold_rows_prediction_columns": confusion,
            "accuracy_headline_including_unresolved": nullable_ratio(correct, total),
            "accuracy_available_predictions_only": nullable_ratio(correct, len(resolved)),
            "alarm_precision": nullable_ratio(true_alarm, pred_alarm),
            "alarm_recall": nullable_ratio(true_alarm, n_alarm),
            "false_alarms_all_gold_non_alarm": nullable_ratio(false_alarms, n_not_alarm),
            "alarm_to_review": nullable_ratio(confusion["alarm"]["review"], n_alarm),
            "alarm_to_no_alert": nullable_ratio(confusion["alarm"]["no_alert"], n_alarm),
            "review_to_alarm": nullable_ratio(confusion["review"]["alarm"], sum(r["gold"] == "review" for r in rows)),
            "review_to_no_alert": nullable_ratio(confusion["review"]["no_alert"], sum(r["gold"] == "review" for r in rows)),
            "alarm_triage_coverage_alarm_or_review": nullable_ratio(sum(r["gold"] == "alarm" and r.get("pred") in ("alarm", "review") for r in rows), n_alarm),
            "review_rate": nullable_ratio(sum(r.get("pred") == "review" for r in rows), total),
            "review_by_reason": review_reasons,
            "technical_review_count": sum(r.get("pred") == "review" and any(x in technical_review_reasons for x in r.get("reasons", [])) for r in rows),
            "by_level": by_level,
        },
        "operations": {
            "asr_failures": sum(bool(r.get("asr_failure")) for r in rows),
            "extraction_or_schema_failures": sum(bool(r.get("extraction_error")) for r in rows),
            "incomplete_extractions": sum(bool(r.get("incomplete_extraction")) for r in rows),
            "grounding_issues": sum(bool(r.get("grounding_issue")) for r in rows),
            "no_final_result": len(unresolved),
            "cache_hits": {"asr": sum(r.get("asr_cache_hit") is True for r in rows),
                           "extraction": sum(r.get("extract_cache_hit") is True for r in rows)},
        },
        "timing_s": {"asr": time_summary([r.get("asr_elapsed_s") for r in rows]),
                     "extraction": time_summary([r.get("extract_elapsed_s") for r in rows]),
                     "decision_output": time_summary([r.get("decision_elapsed_s") for r in rows]),
                     "audio_to_result": time_summary([r.get("total_elapsed_s") for r in rows if r.get("asr_elapsed_s") is not None])},
    }
    # Compatibility aliases for the earlier flat summary schema. New consumers should use
    # ``classification`` above, where a zero denominator stays explicit as n/a (``value: null``).
    answer.update({
        "calls": total,
        "accuracy": round(correct / total, 3) if total else None,
        "alarm_to_review (missed escalation, still triaged)": f"{confusion['alarm']['review']}/{n_alarm}",
        "alarm_to_no_alert (missed without triage)": f"{confusion['alarm']['no_alert']}/{n_alarm}",
        "missed_strict (alarm expected, not alarm)": f"{n_alarm - confusion['alarm']['alarm']}/{n_alarm}",
        "missed_lenient (alarm expected, no_alert)": f"{confusion['alarm']['no_alert']}/{n_alarm}",
        "confusion (gold -> pred)": confusion,
    })
    if any("-K" in r["call"] for r in rows):
        answer["paired_audio"] = paired_audio_metrics(rows)
    return answer


def paired_audio_metrics(rows):
    """Report clean/noisy results by dialogue; a pair is never counted as two independent scenarios."""
    grouped = {}
    for row in rows:
        grouped.setdefault(script_name(row["call"]), []).append(row)
    complete, discordant, both_wrong, incomplete, both_correct = 0, 0, 0, 0, 0
    for pair in grouped.values():
        variants = {recording_variant(r["call"]) for r in pair}
        if len(pair) != 2 or variants != {"clean", "noisy"}:
            incomplete += 1
            continue
        complete += 1
        if pair[0].get("pred") != pair[1].get("pred"):
            discordant += 1
        if all(r.get("pred") != r["gold"] for r in pair):
            both_wrong += 1
        if all(r.get("pred") == r["gold"] for r in pair):
            both_correct += 1
    def subset(variant):
        values = [r for r in rows if recording_variant(r["call"]) == variant]
        return {"correct": sum(r.get("pred") == r["gold"] for r in values), "total": len(values),
                "accuracy": nullable_ratio(sum(r.get("pred") == r["gold"] for r in values), len(values))}
    return {"unique_dialogues": len(grouped), "complete_pairs": complete, "incomplete_pairs": incomplete,
            "pairs_prediction_discordant": discordant, "pairs_both_wrong": both_wrong,
            "dialogues_both_variants_correct": both_correct, "clean": subset("clean"), "noisy": subset("noisy")}


def write_rows_csv(path, rows):
    fields = ("recording_id", "dialogue_id", "variant", "split", "gold", "prediction", "reasons",
              "technical_failure", "asr_failure", "extraction_error", "grounding_issue", "unresolved",
              "asr_cache_hit", "extract_cache_hit", "asr_elapsed_s", "extract_elapsed_s",
              "decision_elapsed_s", "total_elapsed_s", "gold_source")
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "|".join(row[field]) if field == "reasons" else row.get(field) for field in fields})


def write_human_audit_checklist(path, rows):
    """Prepare, but never falsely complete, a minimum audio/evidence audit."""
    picks = []
    for gold_label in ("alarm", "no_alert", "review"):
        match = next((r for r in rows if r["gold"] == gold_label), None)
        if match:
            picks.append(match)
    picks.extend(r for r in rows if r.get("variant") == "noisy" and r not in picks)
    picks.extend(r for r in rows if r.get("pred") != r["gold"] and r not in picks)
    picks = picks[:8]
    lines = ["# Human evidence and audio audit — pending", "", "This form is intentionally unfilled. Automatic grounding only verifies quote/text/timestamps; it is not a semantic or audio-fidelity audit.",
             "", "| Recording | Why selected | Evidence checked | Audio listened | Audio/script/gold consistent | Evidence/predicate consistent | Error type | Reviewer | Date |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in picks:
        why = "final-run error" if row.get("pred") != row["gold"] else f"gold {row['gold']}"
        if row.get("variant") == "noisy":
            why += "; noisy sample"
        lines.append(f"| {row['recording_id']} | {why} | pending | no | pending | pending | pending |  |  |")
    lines += ["", "Required coverage: at least one alarm, a boundary-adjacent no_alert, a review, noisy variants, and any final-run error. Where present, include negation, speaker-role, access-validity, and authority cases."]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def threshold_comparison(rows, primary):
    """Counts only cached-fact re-decisions; thresholds never invoke ASR or a chat model."""
    result = {}
    for preset in sorted({name for row in rows for name in row.get("threshold_predictions", {})}):
        predictions = [row["threshold_predictions"].get(preset) for row in rows]
        result[preset] = {"labels": {label: predictions.count(label) for label in LABELS},
                          "changed_from_primary": [row["recording_id"] for row in rows
                                                   if row["threshold_predictions"].get(preset) != row.get("pred")]}
    return {"primary": primary, "presets": result,
            "note": "Recomputed from the same cached extraction; ASR quality is an escalation heuristic, not a fraud probability."}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="callguard.pipeline")
    ap.add_argument("command", choices=["text", "audio", "eval", "freeze"])
    ap.add_argument("--audio", action="store_true", help="eval: run on the 42 WAVs (Whisper transcripts) instead of the scripts")
    ap.add_argument("path", nargs="?")
    ap.add_argument("--no-speakers", action="store_true", help="hide speaker labels, as with raw ASR output")
    ap.add_argument("--profile", help="chat profile from config/models.json")
    ap.add_argument("--threshold", type=float, help="override escalation.min_asr_quality")
    ap.add_argument("--only", help="eval only calls whose name contains one of these comma-separated texts")
    ap.add_argument("--smoke", action="store_true", help="eval only the five-call regression set")
    ap.add_argument("--require-self-hostable", action="store_true",
                    help="fail unless the selected chat and, for audio, ASR profiles are self-hostable")
    ap.add_argument("--workers", type=int, default=1, help="calls evaluated in parallel")
    ap.add_argument("--policies", default=ROOT / "config" / "policies.json")
    ap.add_argument("--keywords", default=DATA / "Stichwortliste.json")
    args = ap.parse_args(argv)
    policies, keywords = ex.load_json(args.policies), ex.load_json(args.keywords)
    try:
        models.check("chat", args.profile, require_self_hostable=args.require_self_hostable)
        if args.require_self_hostable and (args.command == "audio" or args.audio):
            models.check("asr", require_self_hostable=True)
    except models.ModelError as e:
        sys.exit(f"model not configured: {e}")
    opts = dict(speakers=not args.no_speakers, profile=args.profile, threshold=args.threshold)

    if args.command == "freeze":
        p = models.check("chat", args.profile)
        mode = "audio" if args.audio else "nospeakers" if args.no_speakers else "speakers"
        out = ROOT / "tests" / "fixtures" / "replay" / p["model"] / ex.PROMPT_VERSION
        out.mkdir(parents=True, exist_ok=True)
        for path in sorted((DATA / "Transkript").glob("*.txt")):
            src = ex.cache_path(ex.segments_from_transcript(path, speakers=not args.no_speakers), policies, p)
            if not src.exists():
                sys.exit(f"no cached extraction for {path.stem} ({mode}); run eval first")
            shutil.copy(src, out / f"{path.stem}.{mode}.json")
        print(f"froze {mode} extractions into {out.relative_to(ROOT)}")
        return

    if args.command in ("text", "audio"):
        if not args.path:
            ap.error(f"{args.command} needs a file path")
        run = run_text if args.command == "text" else run_audio
        decision, segments = run(args.path, policies, keywords, **opts)
        print(dec.explain(decision, segments))
        print("\nkeyword hits: " + (", ".join(f"{h['keyword']} '{h['phrase']}' {h['segment_id']}" for h in decision["keyword_hits"]) or "none"))
        return

    source = sorted((DATA / "Audio").glob("*.wav")) if args.audio else sorted((DATA / "Transkript").glob("*.txt"))
    run = run_audio if args.audio else run_text
    paths = [p for p in source if (not args.only or any(o in p.stem for o in args.only.split(",")))
             and (not args.smoke or script_name(p.stem) in SMOKE)]
    if not paths:
        sys.exit("no transcripts matched")
    contract, contract_issues = dataset_contract(paths if args.audio else None)
    # A partial smoke/--only run is not expected to have both variants; full audio evaluation is.
    if args.audio and not args.only and not args.smoke and contract_issues:
        sys.exit("dataset contract invalid:\n- " + "\n- ".join(contract_issues))
    started_wall = time.perf_counter()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda p: run(p, policies, keywords, **opts)[0], paths))
    wall_clock_s = round(time.perf_counter() - started_wall, 3)
    finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    mode = "audio" if args.audio else "nospeakers" if args.no_speakers else "speakers"
    out = ROOT / "runs" / f"eval-{time.strftime('%Y%m%d-%H%M%S')}-{mode}"
    out.mkdir(parents=True, exist_ok=True)
    run_id = out.name
    rows = []
    contract_by_id = {record["recording_id"]: record for record in contract}
    for result in results:
        g = gold(script_name(result["call"]))
        contract_row = contract_by_id.get(result["call"], {})
        recording_duration_s = evidence.duration(result["source"]) if args.audio else None
        augment_canonical_result(result, policies, run_id, recording=contract_row,
                                 recording_duration_s=recording_duration_s)
        validate_canonical_result(result, release=True)
        timing = result.get("_evaluation", {})
        all_issues = [issue for event in result.get("events", []) for issue in event.get("issues", [])]
        grounding_issue = any("grounding" in issue for issue in all_issues)
        row = {"call": result["call"], "recording_id": result["call"],
               "dialogue_id": contract_row.get("dialogue_id", script_name(result["call"])),
               "variant": contract_row.get("variant", "unknown"), "split": contract_row.get("split", "development"),
               "gold": g["label"], "prediction": result.get("label"), "pred": result.get("label"),
               "reasons": result.get("reasons", []), "gold_source": contract_row.get("gold_source", str((DATA / "Skript_mit_Sollbewertung" / f"{script_name(result['call'])}.txt").relative_to(ROOT))),
               "asr_failure": bool(timing.get("asr_failure")), "extraction_error": bool(result.get("error")) and not timing.get("asr_failure"),
               "technical_failure": result.get("status") == "failed" or bool(timing.get("asr_failure")) or "technical_uncertainty" in result.get("reasons", []),
               "incomplete_extraction": any("missing or invalid" in issue for issue in all_issues),
               "grounding_issue": grounding_issue,
               "unresolved": result.get("label") not in LABELS,
               "asr_cache_hit": timing.get("asr_cache_hit"), "extract_cache_hit": timing.get("extract_cache_hit"),
               "asr_elapsed_s": timing.get("asr_elapsed_s"), "extract_elapsed_s": timing.get("extract_elapsed_s"),
               "decision_elapsed_s": timing.get("decision_elapsed_s"), "total_elapsed_s": timing.get("total_elapsed_s"),
               "threshold_predictions": timing.get("threshold_labels", {}),
               "evidence_hit": None if args.audio else bool(cited_segments(result) & set(g["evidence"]))}
        rows.append(row)
        (out / f"{result['call']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        mark = "ok  " if g["label"] == result.get("label") else "ERR " if result.get("error") else "MISS"
        print(f"{mark} {result['call']:<16} gold={g['label']:<8} pred={str(result.get('label')):<8} {','.join(result.get('reasons', []))}")
    m = metrics(rows)
    print(json.dumps(m, indent=1))
    manifest = run_manifest(args, policies, keywords, models.check("chat", args.profile),
                            models.check("asr", require_self_hostable=False) if args.audio else None,
                            contract=contract, started_at=started_at, finished_at=finished_at, wall_clock_s=wall_clock_s,
                            run_id=run_id)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {"format": "callguard-evaluation-2", "artifact_status": "current_run", "args": {k: str(v) for k, v in vars(args).items()},
               "dataset_contract": {"records": contract, "issues": contract_issues}, "metrics": m,
               "threshold_comparison": threshold_comparison(rows, policies["escalation"]["default_preset"]), "rows": rows}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    write_rows_csv(out / "recordings.csv", rows)
    errors = [row for row in rows if row["pred"] != row["gold"] or row["technical_failure"] or row["unresolved"]]
    (out / "errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=1), encoding="utf-8")
    write_human_audit_checklist(out / "human-audit-checklist.md", rows)
    if not manifest["self_hostable_compliant"]:
        print("NOTE: this run used a non-self-hostable endpoint (see manifest.json); the README asks for "
              "self-hostable models in the submitted pipeline.", file=sys.stderr)
    print(f"saved {out.relative_to(ROOT)}/ (canonical JSON, manifest, summary, recordings.csv, errors, human audit checklist)")


if __name__ == "__main__":
    main()
