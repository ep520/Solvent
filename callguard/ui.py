"""Review dashboard server: serves dashboard/ plus a read-only API over cached pipeline results.

  python3 -m callguard.ui [--port 8090]

It never calls a model: transcripts come from data/Transcriptions/, extractions from cache/extract/
(filled by `python3 -m callguard.pipeline eval --audio`). Threshold presets are recomputed with the
deterministic decide step, so switching them is instant.
"""
import argparse
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from callguard import asr
from callguard import decide as dec
from callguard import evidence
from callguard import extract as ex
from callguard import models

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
AUDIO = ROOT / "data" / "Audio"
TRANSCRIPTS = ROOT / "data" / "Transcriptions"
RUNS = ROOT / "runs"
DEFAULT_RUN_FILE = RUNS / "_default_run.txt"  # persistent pointer: plain text, one run_dir per line
WAV_NAME = re.compile(r"^[\w-]+\.wav$")
LABELS = {"alarm": "Alarm", "review": "Review", "no_alert": "No alert"}
STATES = {"true": "Supported", "false": "Excluded", "unknown": "Unknown"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml"}

jobs = {}  # call_id -> {"status": "pending|running|done|error", "run_dir": "...", "error": "..."}
jobs_lock = threading.Lock()
REVIEW_THRESHOLDS = {"0.6": 0.6, "0.7": 0.7, "0.8": 0.8}
DEFAULT_REVIEW_THRESHOLD = "0.6"


def _main_event(result):
    alarming = [e for e in result["events"] if e["status"] != "not_applicable"]
    pool = alarming or result["events"]
    return max(pool, key=lambda e: dec.LABEL_RANK[e["label"]], default=None)


def _decisive_refs(event):
    if event is None:
        return []
    if event["status"] == "not_applicable":
        return event["evidence"]
    wanted = "false" if event["status"] == "absent" else "true"
    refs = [r for c in event["conditions"].values() if c["state"] == wanted for r in c["evidence"]]
    unique = {}
    for r in refs:
        unique.setdefault(tuple(r["segment_ids"]), r)
    return sorted(unique.values(), key=lambda r: r["start"] or 0)


def _assessment(result):
    event = _main_event(result)
    if result.get("error"):
        return f"Extraction failed: {result['error']}"
    if event is None:
        return "No candidate event in any policy family." + (" " + "; ".join(result["issues"]) if result["issues"] else "")
    text = f"{event['title']}. {event['summary']}"
    if event.get("reason") == "below_escalation_threshold":
        text += f" ASR quality of the decisive passage is below {result['threshold']} for: {', '.join(event['low_quality'])}."
    elif event.get("reason") == "technical_uncertainty":
        text += " Part of the evidence could not be verified in the transcript."
    others = [e["family"] for e in result["events"] if e is not event and e["status"] != "not_applicable"]
    if others:
        text += f" Other events assessed: {', '.join(others)}."
    return text


def dashboard_call(call, segments, results, default, length, cache_status="current", extraction_version=None,
                   model_input=None):
    """Map canonical results (one per threshold preset) to the shape the dashboard renders."""
    base = results[default]
    event = _main_event(base)
    by_id = {s["id"]: s for s in segments}
    refs = _decisive_refs(event)
    quality = [r["quality"] for r in refs if r.get("quality") is not None]
    actor = event.get("actor", {}) if event else {}
    actor_ids = set(actor.get("segment_ids", [])) if actor.get("status") == "inferred" else set()

    def evidence_item(ref):
        evidence_start, evidence_end = ref["start"] or 0, ref["end"] or 0
        clip_start, clip_end = evidence.window(evidence_start, evidence_end, length)
        segment_ids = ref["segment_ids"]
        # Prefer the stored ASR text for cited segments; when the transcript isn't available
        # locally (segments missing) this still has the decision's own grounded quote to show
        # rather than hiding the evidence entirely.
        resolved = [by_id[i]["text"] for i in segment_ids if i in by_id]
        text = " ".join(resolved) if resolved else ref["quote"]
        return {
            # `time`, `end` and `segments` remain as compatibility aliases for
            # the static mock; live data uses the explicit canonical fields.
            "time": evidence_start, "end": evidence_end, "segments": segment_ids,
            "segment_ids": segment_ids, "evidence_start": evidence_start, "evidence_end": evidence_end,
            "clip_start": clip_start, "clip_end": clip_end,
            "speaker": "Speaker not identified",
            "text": text, "support": ref["quote"], "transcriptResolved": bool(resolved),
            "quality": ref.get("quality"), "match": ref.get("match"),
            "actorRole": actor["role"] if actor_ids.intersection(segment_ids) else None,
        }

    items = [evidence_item(ref) for ref in refs]
    if event is not None and event["conditions"]:
        conditions = [{"label": n.replace("_", " ").capitalize(), "state": STATES[c["state"]]}
                      for n, c in event["conditions"].items()]
    elif event is not None:
        conditions = [{"label": f"Object type: {event.get('object_type', 'unknown').replace('_', ' ')}", "state": "Supported"}]
    else:
        conditions = []
    open_questions = [q for e in base["events"] if e.get("reason") == "missing_policy_fact" for q in e["open_questions"]]
    grounding_issues = [issue for event_item in base["events"]
                        for condition in event_item.get("conditions", {}).values()
                        for issue in condition.get("grounding_issues", [])]
    grounding_issues += [issue for event_item in base["events"] for issue in event_item.get("issues", [])
                         if "grounding failed" in issue]
    level = re.search(r"Stufe(\d)", call)
    take = call.rsplit("-", 1)[-1]
    return {
        "id": call, "date": f"Level {level.group(1)}" if level else "Call",
        "time": "noisy audio" if take in ("K2", "K4") else "clean audio", "duration": round(length),
        "cacheStatus": cache_status, "extractionVersion": extraction_version,
        "runStatus": base.get("status", "ok"), "policiesVersion": base.get("policies_version"),
        "disabledFamilies": base.get("disabled_families", []),
        "family": event["family"].capitalize() if event else "None",
        "actor": {"role": actor.get("role", "unknown"), "status": actor.get("status", "unknown")},
        # This is an ASR heuristic for the supporting passage, not a calibrated
        # probability or a compliance-decision confidence score.
        "asrQuality": round(min(quality) * 100) if quality else None,
        "classificationByThreshold": {name: LABELS[r["label"]] for name, r in results.items()},
        "assessmentByThreshold": {name: _assessment(r) for name, r in results.items()},
        "reasonByThreshold": {name: r["reasons"] for name, r in results.items()},
        # XAI: "what would have to be different" for the main event, per threshold preset.
        "counterfactualsByThreshold": {name: [{"text": c["text"], "label": LABELS[c["label"]], "kind": c["kind"],
                                               "condition": c.get("condition", "").replace("_", " ").capitalize(),
                                               "from": STATES.get(c.get("from")), "to": STATES.get(c.get("to"))}
                                              for c in ((_main_event(r) or {}).get("counterfactuals") or [])]
                                       for name, r in results.items()},
        "missingFact": " / ".join(open_questions) or None,
        "groundingIssues": list(dict.fromkeys(grounding_issues)),
        "evidence": items or [{"time": 0, "end": 0, "speaker": "", "text": "No supporting passage: no candidate event.",
                               "support": "", "segments": [], "segment_ids": [], "evidence_start": 0,
                               "evidence_end": 0, "clip_start": 0, "clip_end": 0, "quality": None, "match": None}],
        "transcript": [{"time": s["start"], "speaker": "—", "text": s["text"]} for s in segments],
        "conditions": conditions,
        "events": [{"family": e["family"], "label": LABELS[e["label"]], "status": e["status"], "reason": e["reason"],
                    "actor": e.get("actor", {"role": "unknown", "status": "unknown"})}
                   for e in base["events"]],
        "issues": base["issues"],
        # What the extraction model read (snippets with hits, scores and bold spans, or the full transcript),
        # and which of those segments the decision cites as evidence.
        "modelInput": model_input,
        "citedSegments": sorted({sid for e in base["events"] for c in e.get("conditions", {}).values()
                                 for r in c["evidence"] for sid in r["segment_ids"]} |
                                {sid for e in base["events"] for r in e.get("evidence", []) for sid in r["segment_ids"]}),
    }


def keyword_groups(keywords):
    groups = {}
    by_id = {k["id"]: k for k in keywords["keywords"] if k.get("enabled", True)}
    for name, fam in keywords["families"].items():
        phrases = [p for kid in fam["keywords"] if kid in by_id for p in by_id[kid].get("de", []) + by_id[kid].get("gsw", [])]
        groups[name.capitalize()] = list(dict.fromkeys(phrases))
    return groups


def keyword_coverage(keywords, hits, transcript_calls):
    """Return configuration and observed-match coverage; never feeds decisions."""
    enabled = [keyword for keyword in keywords["keywords"] if keyword.get("enabled", True)]
    enabled_ids = {keyword["id"] for keyword in enabled}
    variants = {language: sum(len(keyword.get(language, [])) for keyword in enabled) for language in ("de", "gsw")}
    terms = {" ".join(term.casefold().split())
             for keyword in enabled for language in ("de", "gsw") for term in keyword.get(language, [])}
    by_keyword = {keyword["id"]: keyword for keyword in enabled}
    family_rows = []
    for family, definition in keywords["families"].items():
        ids = [keyword_id for keyword_id in definition.get("keywords", []) if keyword_id in enabled_ids]
        family_hits = [hit for hit in hits if hit["keyword"] in ids]
        family_rows.append({"id": family, "label": definition.get("label", family.title()),
                            "enabledKeywords": len(ids),
                            "variants": sum(len(by_keyword[keyword_id].get(language, []))
                                            for keyword_id in ids for language in ("de", "gsw")),
                            "occurrences": len(family_hits)})
    return {"families": len(keywords["families"]), "enabledKeywords": len(enabled),
            "configuredVariants": variants["de"] + variants["gsw"], "deVariants": variants["de"],
            "gswVariants": variants["gsw"], "distinctTerms": len(terms), "occurrences": len(hits),
            "callsWithOccurrences": len({hit["call"] for hit in hits}), "transcriptCalls": transcript_calls,
            "byFamily": family_rows}


def _clean_terms(value, keyword_id, language, seen):
    if not isinstance(value, list):
        raise ValueError(f"{keyword_id}.{language} must be a list")
    cleaned, local = [], set()
    for raw in value:
        if not isinstance(raw, str) or not (term := " ".join(raw.split())):
            raise ValueError(f"{keyword_id}.{language} contains an empty term")
        normalized = term.casefold()
        if normalized in local:
            raise ValueError(f"{keyword_id}.{language} contains duplicate term {term!r}")
        # The same spelling in DE and Swiss German is legitimate for one keyword;
        # reusing it under another keyword would duplicate match counts.
        if normalized in seen and seen[normalized] != keyword_id:
            raise ValueError(f"duplicate term {term!r} in {keyword_id} and {seen[normalized]}")
        local.add(normalized)
        seen[normalized] = keyword_id
        cleaned.append(term)
    return cleaned


def validate_keyword_config(payload):
    """Validate a UI-edited keyword file before it reaches disk."""
    if not isinstance(payload, dict) or not isinstance(payload.get("keywords"), list) or not isinstance(payload.get("families"), dict):
        raise ValueError("keywords and families are required")
    result = json.loads(json.dumps(payload))  # JSON-only deep copy
    ids, seen = set(), {}
    for keyword in result["keywords"]:
        if not isinstance(keyword, dict) or not isinstance(keyword.get("id"), str) or not keyword["id"].strip():
            raise ValueError("every keyword needs a non-empty id")
        keyword["id"] = keyword["id"].strip()
        if keyword["id"] in ids:
            raise ValueError(f"duplicate keyword id {keyword['id']!r}")
        ids.add(keyword["id"])
        if not isinstance(keyword.get("label"), str) or not keyword["label"].strip():
            raise ValueError(f"{keyword['id']} needs a non-empty label")
        keyword["label"] = " ".join(keyword["label"].split())
        keyword["de"] = _clean_terms(keyword.get("de", []), keyword["id"], "de", seen)
        keyword["gsw"] = _clean_terms(keyword.get("gsw", []), keyword["id"], "gsw", seen)
        if not keyword["de"] and not keyword["gsw"]:
            raise ValueError(f"{keyword['id']} needs at least one term")
        keyword["enabled"] = bool(keyword.get("enabled", True))
    referenced = []
    for family, definition in result["families"].items():
        if not isinstance(definition, dict) or not isinstance(definition.get("keywords"), list):
            raise ValueError(f"family {family!r} needs a keyword list")
        family_ids = definition["keywords"]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError(f"family {family!r} has duplicate keyword IDs")
        unknown = set(family_ids) - ids
        if unknown:
            raise ValueError(f"family {family!r} references unknown IDs: {', '.join(sorted(unknown))}")
        referenced += family_ids
    ungrouped = ids - set(referenced)
    if ungrouped:
        raise ValueError(f"keywords must belong to a family: {', '.join(sorted(ungrouped))}")
    return result


def save_keyword_config(payload, path):
    """Atomically persist a validated keyword configuration supplied by the local dashboard."""
    result = validate_keyword_config(payload)
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return result


def find_extraction(segments, policies, chat, view, cache_dir=ex.ROOT / "cache" / "extract"):
    """Cached extraction for this call, newest input first: (path, status) or (None, None).

    current        extracted with today's input (snippet settings and keyword list)
    stale_input    extracted with an earlier keyword list or snippet setting; re-run the pipeline to refresh
    full_transcript extracted from the whole call (before snippet mode, or snippet mode switched off)
    legacy         an extract-4 cache entry
    """
    path = ex.cache_path(segments, policies, chat, cache_dir, view=view)
    if path.exists():
        return path, "current"
    pointer = ex.latest_path(segments, policies, chat, cache_dir)
    if pointer.exists():
        path = pointer.parent.parent / json.loads(pointer.read_text(encoding="utf-8"))["cache"]
        if path.exists():
            sent = ex.load_json(path).get("_meta", {}).get("input") or {}
            return path, "stale_input" if sent.get("mode") == "snippets" else "full_transcript"
    path = ex.cache_path(segments, policies, chat, cache_dir)
    if path.exists():
        return path, "full_transcript"
    path = ex.legacy_cache_path(segments, policies, chat, cache_dir)
    return (path, "legacy") if path.exists() else (None, None)


def model_input(extraction, current_view, segments):
    """What the model actually read for this extraction, shaped for the dashboard."""
    sent = extraction.get("_meta", {}).get("input")
    if sent is None:
        sent = {"mode": "full_transcript", "reason": "extracted from the whole call (before snippet mode)"}
    out = {"mode": sent["mode"], "reason": sent.get("reason"), "stats": sent.get("stats"),
           "version": sent.get("version"), "config": sent.get("config"),
           "snippets": sent.get("snippets", []), "segmentCount": len(segments)}
    if current_view and current_view.get("mode") == "snippets" and sent != current_view:
        out["currentDiffers"] = True     # keyword list or snippet settings changed since this extraction
    return out


def _atomic_write_json(path, data):
    """Write JSON through a temp file then rename over the target -- atomic on the same filesystem,
    so a reader never observes a partially-written file, and a crash mid-write leaves the previous
    (or no) file intact rather than a truncated one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)  # os.rename: atomic within the same filesystem


def run_inference_job(call_id, policies, keywords, shared_run_dir=None, force_fresh=False):
    """Execute ASR and Qwen inference for a recording and save a SELF-CONTAINED set of artifacts
    for it under run_dir/<call_id>/, plus a run-owned audio copy and the canonical flat
    run_dir/<call_id>.json the dashboard reads. The run can be reopened later without data/
    Transcriptions, cache/extract, or the model services: everything needed is under run_dir.

    shared_run_dir: when set (by a batch caller), all calls in that batch land in the same
    directory, created once by the caller. When None (a single analyze), this call gets its own
    fresh run directory, so repeated single analyses never land in the same directory and never
    silently overwrite an earlier call's artifacts.
    force_fresh: bypass both the ASR and extraction caches (asr.transcribe(force=True) /
    ex.extract(force=True), both existing, supported bypass parameters -- no cache is deleted).

    Each call gets its own run_dir/<call_id>/ subtree, so concurrent jobs in the same shared_run_dir
    never write to the same path; the one genuinely shared file (the jobs dict) is already
    protected by jobs_lock.
    """
    global jobs

    RUNS.mkdir(parents=True, exist_ok=True)
    if shared_run_dir is not None:
        run_dir = shared_run_dir
    else:
        run_dir = RUNS / f"eval-{time.strftime('%Y%m%d-%H%M%S')}-audio"
    run_dir.mkdir(parents=True, exist_ok=True)
    call_dir = run_dir / call_id
    call_dir.mkdir(parents=True, exist_ok=True)
    status_path = call_dir / "status.json"

    status = {"call_id": call_id, "stage": "started", "error": None,
             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _atomic_write_json(status_path, status)
    with jobs_lock:
        jobs[call_id] = {"status": "running", "run_dir": str(run_dir.relative_to(ROOT)), "error": None}

    try:
        audio_file = AUDIO / f"{call_id}.wav"
        if not audio_file.exists():
            raise FileNotFoundError(f"Audio file not found: {call_id}.wav")

        # Run-owned copy of the exact input audio, hash-verified, so this run stays reopenable
        # even if data/Audio/ later changes.
        audio_dir = run_dir / "audio"
        source_bytes = audio_file.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        run_audio_copy = audio_dir / f"{call_id}.wav"
        audio_dir.mkdir(parents=True, exist_ok=True)
        tmp_audio = audio_dir / f".{call_id}.wav.tmp{os.getpid()}"
        tmp_audio.write_bytes(source_bytes)
        tmp_audio.replace(run_audio_copy)
        if hashlib.sha256(run_audio_copy.read_bytes()).hexdigest() != source_hash:
            raise IOError(f"audio copy verification failed for {call_id}")

        status.update(stage="asr_running")
        _atomic_write_json(status_path, status)

        # Pin the self-hosted ASR profile explicitly, matching the chat profile below.
        # models.resolve("asr", None) would fall back to config/models.json's default profile
        # ("openai" -- paid, NOT self-hosted), which is not acceptable for this pipeline and
        # would silently diverge from the frozen self-hosted configuration this run records.
        asr_profile_cfg = models.resolve("asr", "whisper_local")
        asr_profile_name = asr_profile_cfg["name"]
        asr_started = time.perf_counter()
        tx_result, asr_cache_hit = asr.transcribe(audio_file, profile=asr_profile_name, force=force_fresh)
        asr_elapsed = round(time.perf_counter() - asr_started, 3)
        segments = [{**s, "speaker": None} for s in tx_result["segments"]]
        if not segments:
            raise ValueError(f"ASR returned no segments for {call_id}")

        # Full ASR artifact: segments (text, timestamps, quality), provenance, timing, cache use.
        _atomic_write_json(call_dir / "asr.json", {**tx_result, "source_sha256_verified": source_hash,
                           "asr_cache_hit": asr_cache_hit, "asr_elapsed_s": asr_elapsed,
                           "force_fresh_requested": force_fresh})

        status.update(stage="asr_done", asr_elapsed_s=asr_elapsed, asr_cache_hit=asr_cache_hit)
        _atomic_write_json(status_path, status)

        view = ex.input_view(segments, policies, keywords)

        # Exact LLM input (messages + schema), reconstructed with extract.py's own helpers --
        # not a duplicate implementation, the same functions extract() calls internally.
        chat_profile = models.resolve("chat", "qwen3_fast")
        _atomic_write_json(call_dir / "llm_input.json", {
            "messages": ex.build_messages(segments, policies, view), "schema": ex.build_schema(policies),
            "view": view, "profile": chat_profile["name"], "model": chat_profile["model"],
            "raw_response_note": "callguard.models.chat_json() returns only the parsed JSON object; "
                                 "the raw HTTP response text is not retained by existing infrastructure. "
                                 "extraction.json is that parsed result, saved separately below.",
        })

        status.update(stage="extract_running")
        _atomic_write_json(status_path, status)

        extract_cache_path = ex.cache_path(segments, policies, chat_profile, view=view)
        extract_cache_hit_before = extract_cache_path.exists()
        extraction, error = None, None
        extract_started = time.perf_counter()
        try:
            extraction = ex.extract(segments, policies, profile="qwen3_fast", view=view, force=force_fresh)
        except models.ModelError as e:
            extraction, error = {}, e
        extract_elapsed = round(time.perf_counter() - extract_started, 3)
        extract_cache_hit = extract_cache_hit_before and not force_fresh

        _atomic_write_json(call_dir / "extraction.json", extraction)

        status.update(stage="extract_done", extract_elapsed_s=extract_elapsed, extract_cache_hit=extract_cache_hit,
                     extraction_error=str(error) if error else None)
        _atomic_write_json(status_path, status)

        presets = policies["escalation"]["presets"]
        default = policies["escalation"]["default_preset"]
        decide_started = time.perf_counter()
        result = dec.decide(extraction, segments, policies, threshold=presets[default], error=error)
        decide_elapsed = round(time.perf_counter() - decide_started, 3)
        _atomic_write_json(call_dir / "decision.json", result)

        model_input_data = model_input(extraction, view, segments)
        full_result = {**result}
        full_result.update(
            call=call_id, source=str(run_audio_copy), speakers=False, audio=True,
            keyword_hits=ex.keyword_hits(segments, keywords), input=view,
            modelInput=model_input_data, extraction=extraction,
            provenance={
                "asr": {"profile": asr_profile_name, "model": (asr_profile_cfg or {}).get("model"),
                       "cache_hit": asr_cache_hit, "elapsed_s": asr_elapsed, "force_fresh": force_fresh},
                "chat": {"profile": chat_profile["name"], "model": chat_profile["model"],
                        "cache_hit": extract_cache_hit, "elapsed_s": extract_elapsed, "force_fresh": force_fresh},
                "decide_elapsed_s": decide_elapsed, "source_sha256": source_hash,
                "prompt_version": extraction.get("_meta", {}).get("prompt_version") if isinstance(extraction, dict) else None,
            },
            artifacts={"asr": f"{call_id}/asr.json", "llm_input": f"{call_id}/llm_input.json",
                      "extraction": f"{call_id}/extraction.json", "decision": f"{call_id}/decision.json",
                      "status": f"{call_id}/status.json", "audio": f"audio/{call_id}.wav"},
        )

        # Validate before declaring success: every required artifact exists and parses.
        required = [call_dir / "asr.json", call_dir / "llm_input.json", call_dir / "extraction.json",
                   call_dir / "decision.json", run_audio_copy]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise IOError(f"required artifacts missing after write: {missing}")
        for p in (call_dir / "asr.json", call_dir / "extraction.json", call_dir / "decision.json"):
            json.loads(p.read_text(encoding="utf-8"))

        # The canonical flat file the dashboard reads -- written last, only after validation,
        # so a job is never visible as "done" with an incomplete or unvalidated result.
        _atomic_write_json(run_dir / f"{call_id}.json", full_result)

        status.update(stage="complete", completed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), label=result.get("label"))
        _atomic_write_json(status_path, status)

        with jobs_lock:
            jobs[call_id] = {"status": "done", "run_dir": str(run_dir.relative_to(ROOT)), "error": None}
    except Exception as e:
        status.update(stage="failed", error=str(e), failed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            _atomic_write_json(status_path, status)
        except Exception:
            pass
        with jobs_lock:
            jobs[call_id] = {"status": "error", "run_dir": str(run_dir.relative_to(ROOT)), "error": str(e)}


def validate_run_dir(run_dir_str):
    """Validate that run_dir is a safe path within runs/ directory.

    Returns (Path, error_msg) tuple:
      (path, None) if valid
      (None, error_msg) if invalid
    """
    if not run_dir_str:
        return None, None  # Unset param, not an error

    # Parse the path
    try:
        run_path = Path(run_dir_str).resolve()
    except (ValueError, RuntimeError):
        return None, f"Invalid path: {run_dir_str}"

    runs_root = (ROOT / "runs").resolve()

    # Security: ensure path is under runs/ (prevent symlink escape)
    try:
        run_path.relative_to(runs_root)
    except ValueError:
        return None, f"run_dir must be under runs/ directory, not {run_dir_str}"

    # Check directory exists
    if not run_path.is_dir():
        return None, f"Run directory not found: {run_dir_str}"

    # Check for required artifacts
    result_files = list(run_path.glob("*.json"))
    call_results = [f for f in result_files if f.name not in ("manifest.json", "summary.json", "artifact_index.json", "checksums.json")]
    if not call_results:
        return None, f"No call results found in {run_dir_str}"

    return run_path, None


def list_runs():
    """List valid run directories under runs/, newest first, for the run-history selector.

    Reuses validate_run_dir() so this list and ?run_dir= are governed by the same path rules.
    A run is flagged with a source-integrity warning when either:
      - "source unavailable": a call's recorded source path no longer exists on disk (we don't
        know why -- could be a genuinely removed file, nothing synthetic is claimed), or
      - "duplicate source content": two DIFFERENT call_ids in the same run point to audio files
        with identical bytes -- e.g. one was a copy of another -- detected by content hash, not
        by filename. This catches a stale/contaminated result even if a real, distinct file is
        later restored at one of those paths (so plain path-existence would stop catching it).
    Neither condition is reported as "synthetic" unless that provenance is actually known; the
    label here only states what was observed (path missing / content duplicated).
    """
    if not RUNS.is_dir():
        return []
    out = []
    for d in sorted((p for p in RUNS.iterdir() if p.is_dir()), reverse=True):
        run_path, error = validate_run_dir(str(d))
        if error:
            continue
        call_files = sorted(f for f in run_path.glob("*.json") if f.name not in ("manifest.json", "summary.json", "artifact_index.json", "checksums.json"))
        source_warning = None
        hashes = {}  # content hash -> set of call_ids sharing it
        for f in call_files:
            try:
                loaded = ex.load_json(f)
                source = loaded.get("source") if isinstance(loaded, dict) else None
            except (json.JSONDecodeError, OSError):
                continue
            if not source:
                continue
            source_path = Path(source)
            if not source_path.exists():
                source_warning = "source unavailable"
                continue
            try:
                digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
            except OSError:
                continue
            hashes.setdefault(digest, set()).add(f.stem)
        if source_warning is None and any(len(ids) > 1 for ids in hashes.values()):
            source_warning = "duplicate source content"
        manifest_file = run_path / "manifest.json"
        generated_at = None
        if manifest_file.exists():
            try:
                loaded_manifest = ex.load_json(manifest_file)
                generated_at = loaded_manifest.get("generated_at") if isinstance(loaded_manifest, dict) else None
            except (json.JSONDecodeError, OSError):
                pass
        if not generated_at:
            generated_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(run_path.stat().st_mtime))
        out.append({
            "run_dir": str(run_path.relative_to(ROOT)),
            "generated_at": generated_at,
            "recordings": len(call_files),
            "call_ids": [f.stem for f in call_files],
            "sourceWarning": source_warning,
        })
    return out


def build_data_from_run(run_path, policies, keywords, profile=None):
    """Load dashboard data from an explicit evaluation run.

    The saved result is the literal output of dec.decide() (plus call/source/audio/etc merged in),
    so result["events"]/["label"]/["reasons"]/["issues"] are already in the canonical shape
    dashboard_call() expects. If run_inference_job also stored "extraction" (the raw model output),
    model input detail (snippets found, scores) is reconstructed from its _meta.input. Older runs
    without a stored extraction fall back to an "unavailable" model-input marker, not fabricated data.
    """
    chat = models.resolve("chat", profile)
    calls, manifest_data = [], {}

    # Load manifest if available
    manifest_file = run_path / "manifest.json"
    if manifest_file.exists():
        try:
            manifest_data = ex.load_json(manifest_file)
            if not isinstance(manifest_data, dict):
                manifest_data = {}
        except (json.JSONDecodeError, OSError):
            manifest_data = {}

    # Run-level metadata files (manifest.json, summary.json, and whatever else gets added
    # alongside them -- artifact_index.json, checksums.json, ...) are excluded by SHAPE, not
    # by an ever-growing filename blocklist: a genuine per-call result always has "label" and
    # "events" keys (set unconditionally by dec.decide()); metadata files don't.
    for result_file in sorted(run_path.glob("*.json")):
        call_name = result_file.stem
        result = ex.load_json(result_file)
        if not isinstance(result, dict) or "label" not in result or "events" not in result:
            continue  # not a per-call result: metadata file or a different schema entirely

        # Prefer the run-owned audio so a clean clone can play the recording.
        audio_file = run_path / "audio" / f"{call_name}.wav"
        if not audio_file.exists():
            audio_file = AUDIO / f"{call_name}.wav"
        duration = evidence.duration(audio_file) if audio_file.exists() else 0

        # The retained ASR artifact provides the exact segments needed to recompute the three
        # fixed Review thresholds without a cache, private source data, or model call.
        asr_file = run_path / call_name / "asr.json"
        segments = None
        if asr_file.exists():
            try:
                tx_data = ex.load_json(asr_file)
                loaded_segments = [{**s, "speaker": None} for s in tx_data.get("segments", [])] \
                    if isinstance(tx_data, dict) else []
                segments = loaded_segments or None
            except (json.JSONDecodeError, OSError):
                segments = None
        if segments is None:
            transcript_file = TRANSCRIPTS / f"{call_name}.json"
            if transcript_file.exists():
                try:
                    tx_data = ex.load_json(transcript_file)
                    loaded_segments = [{**s, "speaker": None} for s in tx_data.get("segments", [])] \
                        if isinstance(tx_data, dict) else []
                    segments = loaded_segments or None
                except (json.JSONDecodeError, OSError):
                    segments = None

        # Missing transcript must not hide saved conditions/evidence: proceed with empty
        # segments rather than skipping this call. evidence_item() below falls back to each
        # condition's own grounded quote when a segment_id can't be resolved locally, so the
        # decision/conditions/evidence still render -- only the scrollable full-transcript
        # list and ASR-text-vs-quote distinction are unavailable without the transcript.
        transcript_available = segments is not None
        segments = segments or []

        # result["extraction"] (raw model output) is optional: only runs started via
        # run_inference_job store it. result["events"]/["label"]/etc. are always present
        # (dec.decide() sets them even on error, e.g. label="review" + status="failed").
        extraction = result.get("extraction")
        extraction_available = extraction is not None and result.get("error") is None

        recorded_decision = {
            "label": result.get("label"),
            "status": result.get("status"),
            "reasons": result.get("reasons", []),
            "events": result.get("events", []),
            "issues": result.get("issues", []),
            "disabled_families": result.get("disabled_families", []),
            "policies_version": result.get("policies_version"),
        }
        if extraction_available and transcript_available:
            results = {name: dec.decide(extraction, segments, policies, threshold=value)
                       for name, value in REVIEW_THRESHOLDS.items()}
        else:
            results = {name: recorded_decision for name in REVIEW_THRESHOLDS}

        # model_input() reads extraction._meta.input (what was actually sent to the model);
        # current_view is result["input"] (the view computed for *this* run, for the
        # "currentDiffers" comparison). Without a stored extraction there is nothing to
        # describe, so fall back to an explicit "unavailable" marker, not fabricated stats.
        current_view = result.get("input")
        if extraction_available:
            model_input_detail = model_input(extraction, current_view, segments)
        elif not transcript_available:
            model_input_detail = {"mode": "unavailable", "reason": "Transcript not retained/matched for this run; "
                                  "model input detail (snippets, scores) cannot be reconstructed."}
        else:
            model_input_detail = {"mode": "unavailable", "reason": "Run has no stored extraction; "
                                  "model input detail (snippets, scores) was not retained."}

        call = dashboard_call(
            call_name, segments, results, DEFAULT_REVIEW_THRESHOLD, duration,
            cache_status="run",
            extraction_version=(extraction or {}).get("_meta", {}).get("prompt_version", "unknown"),
            model_input=model_input_detail,
        )
        call["model"] = chat["model"]
        call["keywordHitCount"] = len(result.get("keyword_hits", []))

        # The frontend's full-vs-limited detail view must gate on whether we have decision
        # detail to show (events/conditions/evidence, always present once dec.decide() ran
        # successfully), NOT on whether the raw "extraction" blob was additionally retained
        # (extraction_available above is used only for the separate model-input-detail panel).
        # Schema-validity check, not truthiness: an empty events list is a VALID decision
        # (e.g. a clean no_alert with no triggering condition found), not proof of unavailable
        # data. Only a missing/malformed "events" key means we actually lack decision detail.
        has_decision_detail = isinstance(result.get("events"), list)
        call["extractionDataAvailable"] = has_decision_detail
        call["evidenceAvailable"] = has_decision_detail
        call["eventsAvailable"] = has_decision_detail
        call["transcriptAvailable"] = transcript_available

        # Source path no longer resolves on disk -- we don't know why (could be a genuinely
        # removed file), so this is reported as "unavailable", not assumed synthetic.
        call["sourceMissing"] = bool(result.get("source")) and not Path(result["source"]).exists()

        calls.append(call)

    return {
        "source": "run",
        "singlePresetRun": False,
        "calls": calls,
        "pending": [],
        "thresholds": REVIEW_THRESHOLDS,
        "defaultThreshold": DEFAULT_REVIEW_THRESHOLD,
        "keywordGroups": keyword_groups(keywords),
        "keywordConfig": keywords,
        "keywordCoverage": {"coverage": {}},
        "meta": {
            "chat_model": chat["model"],
            "current_prompt_version": ex.PROMPT_VERSION,
            "extraction_versions": [],
            "legacy_cache_calls": 0,
            "policies": policies.get("version"),
            "run_dir": str(run_path.relative_to(ROOT)),
            "run_generated_at": manifest_data.get("generated_at"),
            "run_git_commit": manifest_data.get("git_commit"),
            "data_source": "evaluation_run",
        }
    }


def build_data(policies, keywords, profile=None):
    chat = models.resolve("chat", profile)
    presets = REVIEW_THRESHOLDS
    default = DEFAULT_REVIEW_THRESHOLD
    calls, pending, versions, all_hits = [], [], [], []
    for wav in sorted(AUDIO.glob("*.wav")):
        transcript = TRANSCRIPTS / f"{wav.stem}.json"
        if not transcript.exists():
            pending.append(wav.stem)
            continue
        segments = [{**s, "speaker": None} for s in json.loads(transcript.read_text(encoding="utf-8"))["segments"]]
        view = ex.input_view(segments, policies, keywords)
        cached, cache_status = find_extraction(segments, policies, chat, view)
        if cached is None:
            pending.append(wav.stem)
            continue
        extraction = ex.load_json(cached)
        results = {name: dec.decide(extraction, segments, policies, threshold=value) for name, value in presets.items()}
        extraction_version = extraction.get("_meta", {}).get("prompt_version", "unversioned")
        versions.append(extraction_version)
        hits = [{**hit, "call": wav.stem} for hit in ex.keyword_hits(segments, keywords)]
        all_hits += hits
        call = dashboard_call(wav.stem, segments, results, default, evidence.duration(wav),
                              cache_status=cache_status, extraction_version=extraction_version,
                              model_input=model_input(extraction, view, segments))
        call["model"] = chat["model"]
        call["keywordHitCount"] = len(hits)
        calls.append(call)
    return {"source": "pipeline", "calls": calls, "pending": pending, "thresholds": presets, "defaultThreshold": default,
            "keywordGroups": keyword_groups(keywords), "keywordConfig": keywords,
            "keywordCoverage": keyword_coverage(keywords, all_hits, len(calls)),
            "meta": {"chat_model": chat["model"], "current_prompt_version": ex.PROMPT_VERSION,
                     "extraction_versions": sorted(set(versions)), "legacy_cache_calls": sum(v != ex.PROMPT_VERSION for v in versions),
                     "policies": policies.get("version")}}


class Handler(BaseHTTPRequestHandler):
    policies_path = ROOT / "config" / "policies.json"
    keywords_path = ROOT / "data" / "Stichwortliste.json"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers routinely cancel buffered/ranged audio requests while
            # seeking or switching tracks.  The response is already complete
            # from the server's perspective, so there is nothing to recover.
            self.close_connection = True

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/favicon.ico":
            return self.send(204)
        if path == "/api/data":
            # Check for run_dir query parameter to enable explicit run loading
            query_params = parse_qs(url.query)
            run_dir_param = query_params.get("run_dir", [None])[0]

            # An explicit ?run_dir= always wins (deep links keep working as before). Absent
            # that, a persistent default run (set via DEFAULT_RUN_FILE, survives backend
            # restarts since it's re-read from disk every request) takes over plain "/";
            # only with neither does this fall back to the old live-cache mode.
            if not run_dir_param and DEFAULT_RUN_FILE.exists():
                pointed = DEFAULT_RUN_FILE.read_text(encoding="utf-8").strip()
                if pointed:
                    run_dir_param = pointed

            if run_dir_param:
                # Explicit (or default-pointed) run selection mode
                run_path, error = validate_run_dir(run_dir_param)
                if error:
                    error_response = {"error": error, "source": "validation"}
                    return self.send(400, json.dumps(error_response, ensure_ascii=False).encode())

                data = build_data_from_run(run_path, ex.load_json(self.policies_path), ex.load_json(self.keywords_path))
            else:
                # Default mode: load from extraction cache
                data = build_data(ex.load_json(self.policies_path), ex.load_json(self.keywords_path))

            return self.send(200, json.dumps(data, ensure_ascii=False).encode())
        if path == "/api/runs":
            return self.send(200, json.dumps({"runs": list_runs()}, ensure_ascii=False).encode())
        if path == "/api/status":
            # Get status of inference jobs
            query_params = parse_qs(url.query)
            call_id = query_params.get("call_id", [None])[0]
            if not call_id:
                return self.send(400, b'{"error": "call_id required"}')
            with jobs_lock:
                status = jobs.get(call_id, {"status": "idle"})
            return self.send(200, json.dumps(status, ensure_ascii=False).encode())
        if path.startswith("/audio/") or path.startswith("/clip/"):
            name = path.split("/", 2)[2]
            if not WAV_NAME.match(name):
                return self.send(404, b'{"error": "not found"}')
            # Self-contained runs own a copy under <run_dir>/audio/; serve from there when the
            # request names a validated run and that copy exists, so playback for such a run
            # never silently depends on (or falls back to) the global data/Audio/ directory.
            # Historical runs without a run-owned copy keep working via the global fallback.
            run_dir_param = parse_qs(url.query).get("run_dir", [None])[0]
            wav = None
            if run_dir_param:
                run_path, error = validate_run_dir(run_dir_param)
                if error:
                    return self.send(400, json.dumps({"error": error}).encode())
                run_owned = (run_path / "audio" / name)
                if run_owned.is_file():
                    wav = run_owned
            if wav is None:
                wav = AUDIO / name
            if not wav.is_file():
                return self.send(404, b'{"error": "not found"}')
            if path.startswith("/clip/"):
                q = parse_qs(url.query)
                try:
                    if "clip_start" in q or "clip_end" in q:
                        start, end = float(q["clip_start"][0]), float(q["clip_end"][0])
                        data, a, b = evidence.clip_window(wav, start, end)
                    else:  # Compatibility for existing bookmarks; new UI sends canonical clip bounds.
                        start, end = float(q["start"][0]), float(q["end"][0])
                        data, a, b = evidence.clip(wav, start, end)
                except (KeyError, ValueError):
                    return self.send(400, b'{"error": "valid clip timestamps are required"}')
                filename = f"{wav.stem}_{a:.0f}-{b:.0f}s.wav"
                return self.send(200, data, "audio/wav", {"Content-Disposition": f'attachment; filename="{filename}"'})
            return self.send_wav(wav)
        target = (DASHBOARD / ("index.html" if path in ("", "/") else path.lstrip("/"))).resolve()
        if DASHBOARD.resolve() not in target.parents or not target.is_file():
            return self.send(404, b"not found", "text/plain")
        self.send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/analyze":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("request body must be between 1 and 1000000 bytes")
                payload = json.loads(self.rfile.read(length))
                call_id = payload.get("call_id")
                if not call_id or not isinstance(call_id, str):
                    raise ValueError("call_id required")

                # Check if file exists
                audio_file = AUDIO / f"{call_id}.wav"
                if not audio_file.exists():
                    return self.send(400, json.dumps({"error": f"Audio file not found: {call_id}.wav"}).encode())

                # Start job in background
                with jobs_lock:
                    if call_id in jobs and jobs[call_id]["status"] == "running":
                        return self.send(409, json.dumps({"error": "Already analyzing"}).encode())
                    jobs[call_id] = {"status": "pending", "run_dir": None, "error": None}

                # Execute in background thread
                policies = ex.load_json(self.policies_path)
                keywords = ex.load_json(self.keywords_path)
                executor = ThreadPoolExecutor(max_workers=1)
                executor.submit(run_inference_job, call_id, policies, keywords)

                return self.send(202, json.dumps({"status": "started", "call_id": call_id}).encode())
            except (ValueError, json.JSONDecodeError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode())
        elif url.path == "/api/analyze-all":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length)) if length > 0 else {}
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")

                # Explicit call_ids (the caller's actual selection, e.g. "the 42 authentic
                # recordings") takes precedence over scanning data/Audio/*.wav, so every
                # selected recording is included -- no silent skip of one whose status in
                # this process's in-memory jobs dict happens to already read "done".
                call_ids = payload.get("call_ids")
                if call_ids is not None:
                    if not isinstance(call_ids, list) or not all(isinstance(c, str) and c for c in call_ids):
                        raise ValueError("call_ids must be a non-empty list of non-empty strings")
                    selected = call_ids
                else:
                    selected = [wav.stem for wav in sorted(AUDIO.glob("*.wav"))]

                missing_audio = [c for c in selected if not (AUDIO / f"{c}.wav").exists()]
                if missing_audio:
                    return self.send(400, json.dumps({"error": f"audio file not found for: {missing_audio}"}).encode())

                force_fresh = bool(payload.get("force_fresh", False))
                max_workers = max(1, min(int(payload.get("max_workers", 2)), 4))  # conservative cap

                # One fresh directory for this whole batch, created once up front and passed
                # explicitly to every job (not a mutable global), so it cannot leak into or be
                # reused by an unrelated later single-recording /api/analyze call.
                batch_dir = RUNS / f"eval-{time.strftime('%Y%m%d-%H%M%S')}-audio"
                batch_dir.mkdir(parents=True, exist_ok=True)

                policies = ex.load_json(self.policies_path)
                keywords = ex.load_json(self.keywords_path)
                executor = ThreadPoolExecutor(max_workers=max_workers)

                with jobs_lock:
                    for call_id in selected:
                        if call_id in jobs and jobs[call_id]["status"] == "running":
                            continue  # already in flight right now; do not double-submit
                        jobs[call_id] = {"status": "pending", "run_dir": str(batch_dir.relative_to(ROOT)), "error": None}
                        executor.submit(run_inference_job, call_id, policies, keywords, batch_dir, force_fresh)

                return self.send(202, json.dumps({"status": "started", "run_dir": str(batch_dir.relative_to(ROOT)),
                                  "selected": selected, "total": len(selected), "force_fresh": force_fresh}).encode())
            except Exception as error:
                return self.send(400, json.dumps({"error": str(error)}).encode())
        elif url.path == "/api/keywords":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 512_000:
                    raise ValueError("request body must be between 1 and 512000 bytes")
                payload = json.loads(self.rfile.read(length))
                saved = save_keyword_config(payload, self.keywords_path)
            except (ValueError, json.JSONDecodeError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode())
            return self.send(200, json.dumps({"ok": True, "keywords": saved}, ensure_ascii=False).encode())
        else:
            return self.send(404, b'{"error": "not found"}')

    def send_wav(self, wav):
        size = wav.stat().st_size
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if not m:
            return self.send(200, wav.read_bytes(), "audio/wav", {"Accept-Ranges": "bytes"})
        start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2)))
        end = int(m.group(2)) if m.group(1) and m.group(2) else size - 1
        if start >= size:
            return self.send(416, b"", "audio/wav", {"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
        with wav.open("rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        self.send(206, body, "audio/wav", {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}"})


def make_server(host="127.0.0.1", port=8090, *, policies_path=None, keywords_path=None):
    """Create a dashboard server, optionally against isolated config files for tests."""
    if policies_path is None and keywords_path is None:
        handler = Handler
    else:
        class handler(Handler):
            pass
        if policies_path is not None:
            handler.policies_path = Path(policies_path)
        if keywords_path is not None:
            handler.keywords_path = Path(keywords_path)
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="callguard.ui")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args(argv)
    try:
        srv = make_server(args.host, args.port)
    except OSError as e:
        raise SystemExit(f"cannot listen on {args.host}:{args.port} ({e.strerror}). The dashboard may already be "
                         f"running at http://{args.host}:{args.port}; otherwise choose another port with --port.")
    print(f"CallGuard dashboard on http://{args.host}:{srv.server_address[1]}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
