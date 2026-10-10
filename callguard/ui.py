"""Review dashboard server: serves dashboard/ plus a read-only API over cached pipeline results.

  python3 -m callguard.ui [--port 8090]

It never calls a model: transcripts come from data/Transcriptions/, extractions from cache/extract/
(filled by `python3 -m callguard.pipeline eval --audio`). Threshold presets are recomputed with the
deterministic decide step, so switching them is instant.
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from callguard import decide as dec
from callguard import evidence
from callguard import extract as ex
from callguard import human_feedback
from callguard import models

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
AUDIO = ROOT / "data" / "Audio"
TRANSCRIPTS = ROOT / "data" / "Transcriptions"
WAV_NAME = re.compile(r"^[\w-]+\.wav$")
CALL_NAME = re.compile(r"^[\w-]+$")
LABELS = {"alarm": "Alarm", "review": "Review", "no_alert": "No alert"}
STATES = {"true": "Supported", "false": "Excluded", "unknown": "Unknown"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml"}


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


def _format_timestamp(seconds):
    """0:00 / 12:34, matching the dashboard's own formatTime() in app.js."""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _condition_reason(condition):
    """Short, operator-facing rationale for a dashboard fact; never a new model judgement."""
    state = condition["state"]
    refs = condition.get("evidence", [])
    if refs:
        ref = refs[0]
        at = f" at {_format_timestamp(ref['start'])}" if isinstance(ref.get("start"), (int, float)) else ""
        quote = ref.get("quote", "").strip()
        prefix = ("The transcript says" if state == "true" else
                  "The transcript explicitly says" if state == "false" else "Relevant transcript context")
        return f'{prefix}: “{quote}”{at}.'
    if condition.get("grounding_issues"):
        return "Not established: the cited passage could not be found in the transcript."
    if condition.get("downgraded"):
        return "Not established: there is no usable supporting passage."
    return "Not established: the transcript neither confirms nor excludes this fact."


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
        return {
            # `time`, `end` and `segments` remain as compatibility aliases for
            # the static mock; live data uses the explicit canonical fields.
            "time": evidence_start, "end": evidence_end, "segments": segment_ids,
            "segment_ids": segment_ids, "evidence_start": evidence_start, "evidence_end": evidence_end,
            "clip_start": clip_start, "clip_end": clip_end,
            "speaker": "Speaker not identified",
            # Always render ASR text as stored, never a normalised quote.
            "text": " ".join(by_id[i]["text"] for i in segment_ids), "support": ref["quote"],
            "quality": ref.get("quality"), "match": ref.get("match"),
            "actorRole": actor["role"] if actor_ids.intersection(segment_ids) else None,
        }

    items = [evidence_item(ref) for ref in refs]
    if event is not None and event["conditions"]:
        conditions = [{"label": n.replace("_", " ").capitalize(), "state": STATES[c["state"]],
                       "policy": c.get("policy", ""), "reason": _condition_reason(c), "hasEvidence": bool(c.get("evidence"))}
                      for n, c in event["conditions"].items()]
    elif event is not None:
        conditions = [{"label": f"Object type: {event.get('object_type', 'unknown').replace('_', ' ')}", "state": "Supported",
                       "policy": "Numbers are classified only (never an alarm condition); this is the applied rule, not an extracted fact.",
                       "reason": "Classification-only identifier rule; this policy family never generates an alarm.", "hasEvidence": bool(event.get("evidence"))}]
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
        "noCandidateEvent": event is None,
        "factsReason": ("No candidate event was extracted for any enabled policy family. This no-alert is an absence of extracted policy facts, not a guarantee that the recording is safe."
                        if event is None else None),
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
        "transcript": [{"id": s["id"], "time": s["start"], "speaker": "—", "text": s["text"]} for s in segments],
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


_POLICY_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_policy_config(payload):
    """Validate editable policy CRUD data before atomically replacing the local file.

    Policy changes alter extraction/decision semantics.  They intentionally do
    not reuse an old extraction cache: callers must run a fresh evaluation.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("families"), dict):
        raise ValueError("policies need a families object")
    result = json.loads(json.dumps(payload))
    if not isinstance(result.get("version"), str) or not result["version"].strip():
        raise ValueError("policies need a non-empty version")
    escalation = result.get("escalation")
    if not isinstance(escalation, dict) or not isinstance(escalation.get("min_asr_quality"), (int, float)):
        raise ValueError("escalation.min_asr_quality must be numeric")
    if not 0 <= escalation["min_asr_quality"] <= 1:
        raise ValueError("escalation.min_asr_quality must be between 0 and 1")
    presets = escalation.get("presets")
    if not isinstance(presets, dict) or not presets:
        raise ValueError("escalation needs one or more presets")
    for name, value in presets.items():
        if not isinstance(name, str) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError("each escalation preset must be a number between 0 and 1")
    if escalation.get("default_preset") not in presets:
        raise ValueError("escalation.default_preset must name a preset")
    if not result["families"]:
        raise ValueError("at least one policy family is required")
    for family_id, family in result["families"].items():
        if not isinstance(family_id, str) or not _POLICY_ID.fullmatch(family_id):
            raise ValueError("policy IDs must use lowercase letters, digits and underscores")
        if not isinstance(family, dict) or not isinstance(family.get("title"), str) or not family["title"].strip():
            raise ValueError(f"policy {family_id!r} needs a title")
        family["title"] = " ".join(family["title"].split())
        family["enabled"] = bool(family.get("enabled", True))
        family["can_alarm"] = bool(family.get("can_alarm", True))
        conditions = family.get("conditions", {})
        if not isinstance(conditions, dict):
            raise ValueError(f"policy {family_id!r} conditions must be an object")
        if family["can_alarm"] and not conditions:
            raise ValueError(f"alarm-capable policy {family_id!r} needs at least one condition")
        for condition_id, explanation in conditions.items():
            if not isinstance(condition_id, str) or not _POLICY_ID.fullmatch(condition_id):
                raise ValueError(f"policy {family_id!r} has an invalid condition ID")
            if not isinstance(explanation, str) or not explanation.strip():
                raise ValueError(f"condition {family_id}.{condition_id} needs an explanation")
            conditions[condition_id] = " ".join(explanation.split())
        roles = family.get("role_requirements", {})
        if not isinstance(roles, dict) or set(roles) - set(conditions):
            raise ValueError(f"policy {family_id!r} has a role requirement for an unknown condition")
        for condition_id, role in roles.items():
            if role not in ("customer", "advisor"):
                raise ValueError(f"role requirement {family_id}.{condition_id} must be customer or advisor")
        object_types = family.get("object_types", [])
        if object_types and (not isinstance(object_types, list) or any(not isinstance(value, str) or not value.strip() for value in object_types)):
            raise ValueError(f"policy {family_id!r} object_types must be non-empty strings")
    return result


def save_policy_config(payload, path):
    """Atomically persist validated local policy CRUD data."""
    result = validate_policy_config(payload)
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return result


def find_extraction(segments, policies, chat, view, cache_dir=ex.ROOT / "cache" / "extract", feedback_examples=None):
    """Cached extraction for this call, newest input first: (path, status) or (None, None).

    current        extracted with today's input (snippet settings and keyword list)
    stale_input    extracted with an earlier keyword list or snippet setting; re-run the pipeline to refresh
    full_transcript extracted from the whole call (before snippet mode, or snippet mode switched off)
    legacy         an extract-4 cache entry
    """
    path = ex.cache_path(segments, policies, chat, cache_dir, view=view, feedback_examples=feedback_examples)
    if path.exists():
        return path, "current"
    pointer = ex.latest_path(segments, policies, chat, cache_dir)
    if pointer.exists():
        path = pointer.parent.parent / json.loads(pointer.read_text(encoding="utf-8"))["cache"]
        if path.exists():
            meta = ex.load_json(path).get("_meta", {})
            if meta.get("review_feedback_fingerprint") != human_feedback.examples_fingerprint(feedback_examples or []):
                return path, "stale_feedback"
            sent = meta.get("input") or {}
            return path, "stale_input" if sent.get("mode") == "snippets" else "full_transcript"
    path = ex.cache_path(segments, policies, chat, cache_dir, feedback_examples=feedback_examples)
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


def build_data(policies, keywords, profile=None, corrections_dir=human_feedback.CORRECTIONS,
               feedback_path=human_feedback.FEEDBACK):
    chat = models.resolve("chat", profile)
    presets = policies["escalation"]["presets"]
    default = policies["escalation"]["default_preset"]
    calls, pending, versions, all_hits = [], [], [], []
    for wav in sorted(AUDIO.glob("*.wav")):
        transcript = TRANSCRIPTS / f"{wav.stem}.json"
        if not transcript.exists():
            pending.append(wav.stem)
            continue
        raw_segments = [{**s, "speaker": None} for s in json.loads(transcript.read_text(encoding="utf-8"))["segments"]]
        segments, corrected = human_feedback.apply_corrections(wav.stem, raw_segments, corrections_dir)
        feedback_examples = human_feedback.calibration_examples(feedback_path, exclude_call=wav.stem)
        view = ex.input_view(segments, policies, keywords)
        cached, cache_status = find_extraction(segments, policies, chat, view, feedback_examples=feedback_examples)
        decision_segments = segments
        # A corrected transcript can still be reviewed against the last raw extraction, but is clearly
        # marked stale until a fresh pipeline run has extracted facts from the corrected wording.
        if cached is None and corrected:
            raw_view = ex.input_view(raw_segments, policies, keywords)
            cached, cache_status = find_extraction(raw_segments, policies, chat, raw_view, feedback_examples=feedback_examples)
            decision_segments = raw_segments
            if cached is not None:
                cache_status = "stale_transcript"
        if cached is None:
            pending.append(wav.stem)
            continue
        extraction = ex.load_json(cached)
        results = {name: dec.decide(extraction, decision_segments, policies, threshold=value) for name, value in presets.items()}
        extraction_version = extraction.get("_meta", {}).get("prompt_version", "unversioned")
        versions.append(extraction_version)
        hits = [{**hit, "call": wav.stem} for hit in ex.keyword_hits(segments, keywords)]
        all_hits += hits
        call = dashboard_call(wav.stem, segments, results, default, evidence.duration(wav),
                              cache_status=cache_status, extraction_version=extraction_version,
                              model_input=model_input(extraction, view, segments))
        call["model"] = chat["model"]
        call["keywordHitCount"] = len(hits)
        call["transcriptCorrections"] = corrected
        call["humanFeedback"] = human_feedback.review_for_call(wav.stem, feedback_path)
        calls.append(call)
    return {"source": "pipeline", "calls": calls, "pending": pending, "thresholds": presets, "defaultThreshold": default,
            "keywordGroups": keyword_groups(keywords), "keywordConfig": keywords,
            "policyConfig": policies,
            "keywordCoverage": keyword_coverage(keywords, all_hits, len(calls)),
            "meta": {"chat_model": chat["model"], "current_prompt_version": ex.PROMPT_VERSION,
                     "extraction_versions": sorted(set(versions)), "legacy_cache_calls": sum(v != ex.PROMPT_VERSION for v in versions),
                     "policies": policies.get("version")}}


class Handler(BaseHTTPRequestHandler):
    policies_path = ROOT / "config" / "policies.json"
    keywords_path = ROOT / "data" / "Stichwortliste.json"
    corrections_dir = human_feedback.CORRECTIONS
    feedback_path = human_feedback.FEEDBACK

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
            data = build_data(ex.load_json(self.policies_path), ex.load_json(self.keywords_path),
                              corrections_dir=self.corrections_dir, feedback_path=self.feedback_path)
            return self.send(200, json.dumps(data, ensure_ascii=False).encode())
        if path.startswith("/audio/") or path.startswith("/clip/"):
            name = path.split("/", 2)[2]
            wav = AUDIO / name
            if not WAV_NAME.match(name) or not wav.is_file():
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
        if url.path not in ("/api/keywords", "/api/policies", "/api/reviews") and not url.path.startswith("/api/transcripts/"):
            return self.send(404, b'{"error": "not found"}')
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 512_000:
                raise ValueError("request body must be between 1 and 512000 bytes")
            payload = json.loads(self.rfile.read(length))
            if url.path == "/api/keywords":
                saved = save_keyword_config(payload, self.keywords_path)
                key = "keywords"
            elif url.path == "/api/policies":
                saved = save_policy_config(payload, self.policies_path)
                key = "policies"
            elif url.path == "/api/reviews":
                saved = self.save_review(payload)
                key = "review"
            else:
                saved = self.save_transcript_correction(url.path, payload)
                key = "transcript"
        except (ValueError, json.JSONDecodeError) as error:
            return self.send(400, json.dumps({"error": str(error)}).encode())
        return self.send(200, json.dumps({"ok": True, key: saved}, ensure_ascii=False).encode())

    def transcript_segments(self, call):
        if not CALL_NAME.fullmatch(call):
            raise ValueError("invalid call id")
        path = TRANSCRIPTS / f"{call}.json"
        if not path.is_file():
            raise ValueError("transcript not found")
        return [{**segment, "speaker": None} for segment in ex.load_json(path).get("segments", [])]

    def save_transcript_correction(self, endpoint, payload):
        call = endpoint.removeprefix("/api/transcripts/")
        if not isinstance(payload, dict):
            raise ValueError("a transcript correction object is required")
        segments = self.transcript_segments(call)
        saved = human_feedback.save_correction(call, segments, payload.get("segment_id"), payload.get("text"),
                                                self.corrections_dir)
        corrected, changed = human_feedback.apply_corrections(call, segments, self.corrections_dir)
        return {"call": call, "corrected_segments": changed,
                "segment": next(segment for segment in corrected if segment["id"] == payload["segment_id"]),
                "document": saved}

    def save_review(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("a review object is required")
        call, threshold = payload.get("call"), payload.get("threshold")
        if not isinstance(call, str) or not isinstance(threshold, str):
            raise ValueError("call and threshold are required")
        policies, keywords = ex.load_json(self.policies_path), ex.load_json(self.keywords_path)
        data = build_data(policies, keywords, corrections_dir=self.corrections_dir, feedback_path=self.feedback_path)
        current = next((item for item in data["calls"] if item["id"] == call), None)
        if current is None or current["classificationByThreshold"].get(threshold) != "Review":
            raise ValueError("human outcome can only resolve a current review")
        segments = self.transcript_segments(call)
        segments, _ = human_feedback.apply_corrections(call, segments, self.corrections_dir)
        return human_feedback.save_review(call, payload.get("outcome"), current.get("family"),
                                          current.get("reasonByThreshold", {}).get(threshold, []), segments,
                                          self.feedback_path)

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


def make_server(host="127.0.0.1", port=8090, *, policies_path=None, keywords_path=None, corrections_dir=None, feedback_path=None):
    """Create a dashboard server, optionally against isolated config files for tests."""
    if policies_path is None and keywords_path is None and corrections_dir is None and feedback_path is None:
        handler = Handler
    else:
        class handler(Handler):
            pass
        if policies_path is not None:
            handler.policies_path = Path(policies_path)
        if keywords_path is not None:
            handler.keywords_path = Path(keywords_path)
        if corrections_dir is not None:
            handler.corrections_dir = Path(corrections_dir)
        if feedback_path is not None:
            handler.feedback_path = Path(feedback_path)
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
