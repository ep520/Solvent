"""Persistent human review outcomes and transcript-correction overlays.

The original ASR export is never edited. Corrections live beside it and are
applied at read time, so a later ASR export cannot silently discard a human
change. Human review outcomes are small, auditable calibration examples for a
future extraction prompt; they never override the deterministic decision for a
current call.
"""
import hashlib
import json
import time
from pathlib import Path

from callguard import storage

ROOT = Path(__file__).resolve().parent.parent
CORRECTIONS = ROOT / "data" / "Corrections"
FEEDBACK = ROOT / "data" / "ReviewFeedback" / "reviews.json"
OUTCOMES = {"alarm", "no_alert"}


def _timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def correction_path(call, corrections_dir=CORRECTIONS):
    return Path(corrections_dir) / f"{call}.json"


def apply_corrections(call, segments, corrections_dir=CORRECTIONS):
    """Return copied segments with compatible human corrections overlaid.

    A correction is ignored when its stored original text no longer matches the
    ASR segment. This prevents an old edit from being applied to a new ASR run.
    """
    document = _read_json(correction_path(call, corrections_dir), {"corrections": {}})
    corrections = document.get("corrections", {}) if isinstance(document, dict) else {}
    changed, output = [], []
    for segment in segments:
        item = dict(segment)
        correction = corrections.get(str(segment.get("id")), {})
        if (isinstance(correction, dict) and correction.get("original_text") == segment.get("text")
                and isinstance(correction.get("text"), str) and correction["text"].strip()):
            item["text"] = correction["text"]
            changed.append(item["id"])
        output.append(item)
    return output, changed


def save_correction(call, segments, segment_id, text, corrections_dir=CORRECTIONS):
    """Save one text correction, or remove it when it equals the original ASR text."""
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("a transcript segment id is required")
    if not isinstance(text, str) or not (text := text.strip()):
        raise ValueError("corrected transcript text cannot be empty")
    if len(text) > 10_000:
        raise ValueError("corrected transcript text is too long")
    original = next((segment for segment in segments if segment.get("id") == segment_id), None)
    if original is None:
        raise ValueError("transcript segment does not exist")
    path = correction_path(call, corrections_dir)
    document = _read_json(path, {"format": "callguard-transcript-corrections-v1", "call": call, "corrections": {}})
    if not isinstance(document, dict) or document.get("call") not in (None, call):
        raise ValueError("invalid correction file")
    corrections = document.setdefault("corrections", {})
    if text == original["text"]:
        corrections.pop(segment_id, None)
    else:
        corrections[segment_id] = {"original_text": original["text"], "text": text, "updated_at": _timestamp()}
    document.update({"format": "callguard-transcript-corrections-v1", "call": call, "updated_at": _timestamp(),
                     "corrections": corrections})
    storage.atomic_write_json(path, document, indent=2)
    return document


def _feedback_document(path=FEEDBACK):
    document = _read_json(path, {"format": "callguard-review-feedback-v1", "reviews": {}})
    if not isinstance(document, dict) or not isinstance(document.get("reviews"), dict):
        raise ValueError("invalid review feedback file")
    return document


def save_review(call, outcome, family, reasons, segments, feedback_path=FEEDBACK):
    """Store the resolved review as an auditable, bounded calibration example."""
    if outcome not in OUTCOMES:
        raise ValueError("review outcome must be alarm or no_alert")
    if not isinstance(call, str) or not call:
        raise ValueError("a call id is required")
    document = _feedback_document(feedback_path)
    document["reviews"][call] = {
        "outcome": outcome, "family": str(family or "unknown"),
        "review_reasons": [str(reason) for reason in reasons if isinstance(reason, str)],
        "segments": [{"id": str(segment.get("id")), "text": str(segment.get("text", ""))}
                     for segment in segments if segment.get("id") and segment.get("text")],
        "updated_at": _timestamp(),
    }
    document["format"] = "callguard-review-feedback-v1"
    storage.atomic_write_json(feedback_path, document, indent=2)
    return document["reviews"][call]


def review_for_call(call, feedback_path=FEEDBACK):
    return _feedback_document(feedback_path)["reviews"].get(call)


def calibration_examples(feedback_path=FEEDBACK, exclude_call=None, limit=6):
    """Return only recent, structurally valid examples for the extraction prompt."""
    reviews = _feedback_document(feedback_path)["reviews"]
    usable = []
    for call, item in reviews.items():
        if call == exclude_call or not isinstance(item, dict) or item.get("outcome") not in OUTCOMES:
            continue
        segments = item.get("segments")
        if not isinstance(segments, list) or not segments:
            continue
        usable.append({"call": call, "outcome": item["outcome"], "family": item.get("family", "unknown"),
                       "review_reasons": item.get("review_reasons", []), "segments": segments,
                       "updated_at": item.get("updated_at", "")})
    usable.sort(key=lambda item: (item["updated_at"], item["call"]), reverse=True)
    return usable[:limit]


def examples_fingerprint(examples):
    payload = json.dumps(examples, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def prompt_examples(examples):
    """Render compact examples without allowing them to become current-call evidence."""
    rendered = []
    for example in examples:
        pieces, remaining = [], 900
        for segment in example["segments"]:
            text = " ".join(segment.get("text", "").split())
            if not text or remaining <= 0:
                continue
            text = text[:remaining]
            pieces.append(f"[{segment.get('id', '?')}] {text}")
            remaining -= len(text)
        outcome = "alert" if example["outcome"] == "alarm" else "no alert"
        rendered.append(f"Past human resolution: {outcome}; family: {example['family']}; "
                        f"automatic review reasons: {', '.join(example['review_reasons']) or 'unspecified'}.\n"
                        + "\n".join(pieces))
    return rendered
