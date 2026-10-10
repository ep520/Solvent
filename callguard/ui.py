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
from callguard import models

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
AUDIO = ROOT / "data" / "Audio"
TRANSCRIPTS = ROOT / "data" / "Transcriptions"
WAV_NAME = re.compile(r"^[\w-]+\.wav$")
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


def dashboard_call(call, segments, results, default, length):
    """Map canonical results (one per threshold preset) to the shape the dashboard renders."""
    base = results[default]
    event = _main_event(base)
    by_id = {s["id"]: s for s in segments}
    refs = _decisive_refs(event)
    quality = [r["quality"] for r in refs if r.get("quality") is not None]
    items = [{"time": r["start"] or 0, "end": r["end"] or 0, "speaker": "Speaker not identified",
              "text": " ".join(by_id[i]["text"] for i in r["segment_ids"]), "support": r["quote"],
              "segments": r["segment_ids"], "quality": r.get("quality"),
              "match": r.get("match")} for r in refs]
    if event is not None and event["conditions"]:
        conditions = [{"label": n.replace("_", " ").capitalize(), "state": STATES[c["state"]]}
                      for n, c in event["conditions"].items()]
    elif event is not None:
        conditions = [{"label": f"Object type: {event.get('object_type', 'unknown').replace('_', ' ')}", "state": "Supported"}]
    else:
        conditions = []
    open_questions = [q for e in base["events"] if e.get("reason") == "missing_policy_fact" for q in e["open_questions"]]
    level = re.search(r"Stufe(\d)", call)
    take = call.rsplit("-", 1)[-1]
    return {
        "id": call, "date": f"Level {level.group(1)}" if level else "Call",
        "time": "noisy audio" if take in ("K2", "K4") else "clean audio", "duration": round(length),
        "family": event["family"].capitalize() if event else "None",
        "confidence": round(min(quality) * 100) if quality else None,
        "classificationByThreshold": {name: LABELS[r["label"]] for name, r in results.items()},
        "assessmentByThreshold": {name: _assessment(r) for name, r in results.items()},
        "reasonByThreshold": {name: r["reasons"] for name, r in results.items()},
        "missingFact": " / ".join(open_questions) or None,
        "evidence": items or [{"time": 0, "end": 0, "speaker": "", "text": "No supporting passage: no candidate event.",
                               "support": "", "segments": [], "quality": None, "match": None}],
        "transcript": [{"time": s["start"], "speaker": "—", "text": s["text"]} for s in segments],
        "conditions": conditions,
        "events": [{"family": e["family"], "label": LABELS[e["label"]], "status": e["status"], "reason": e["reason"]}
                   for e in base["events"]],
        "issues": base["issues"],
    }


def keyword_groups(keywords):
    groups = {}
    by_id = {k["id"]: k for k in keywords["keywords"] if k.get("enabled", True)}
    for name, fam in keywords["families"].items():
        phrases = [p for kid in fam["keywords"] if kid in by_id for p in by_id[kid].get("de", []) + by_id[kid].get("gsw", [])]
        groups[name.capitalize()] = list(dict.fromkeys(phrases))
    return groups


def build_data(policies, keywords, profile=None):
    chat = models.resolve("chat", profile)
    presets = policies["escalation"]["presets"]
    default = policies["escalation"]["default_preset"]
    calls, pending = [], []
    for wav in sorted(AUDIO.glob("*.wav")):
        transcript = TRANSCRIPTS / f"{wav.stem}.json"
        if not transcript.exists():
            pending.append(wav.stem)
            continue
        segments = [{**s, "speaker": None} for s in json.loads(transcript.read_text(encoding="utf-8"))["segments"]]
        cached = ex.cache_path(segments, policies, chat)
        if not cached.exists():
            pending.append(wav.stem)
            continue
        extraction = ex.load_json(cached)
        results = {name: dec.decide(extraction, segments, policies, threshold=value) for name, value in presets.items()}
        calls.append(dashboard_call(wav.stem, segments, results, default, evidence.duration(wav)))
    return {"source": "pipeline", "calls": calls, "pending": pending, "thresholds": presets, "defaultThreshold": default,
            "keywordGroups": keyword_groups(keywords),
            "meta": {"chat_model": chat["model"], "prompt_version": ex.PROMPT_VERSION, "policies": policies.get("version")}}


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
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/favicon.ico":
            return self.send(204)
        if path == "/api/data":
            data = build_data(ex.load_json(self.policies_path), ex.load_json(self.keywords_path))
            return self.send(200, json.dumps(data, ensure_ascii=False).encode())
        if path.startswith("/audio/") or path.startswith("/clip/"):
            name = path.split("/", 2)[2]
            wav = AUDIO / name
            if not WAV_NAME.match(name) or not wav.is_file():
                return self.send(404, b'{"error": "not found"}')
            if path.startswith("/clip/"):
                q = parse_qs(url.query)
                try:
                    start, end = float(q["start"][0]), float(q["end"][0])
                except (KeyError, ValueError):
                    return self.send(400, b'{"error": "start and end are required"}')
                data, a, b = evidence.clip(wav, start, end)
                filename = f"{wav.stem}_{a:.0f}-{b:.0f}s.wav"
                return self.send(200, data, "audio/wav", {"Content-Disposition": f'attachment; filename="{filename}"'})
            return self.send_wav(wav)
        target = (DASHBOARD / ("index.html" if path in ("", "/") else path.lstrip("/"))).resolve()
        if DASHBOARD.resolve() not in target.parents or not target.is_file():
            return self.send(404, b"not found", "text/plain")
        self.send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

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


def make_server(host="127.0.0.1", port=8090):
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="callguard.ui")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args(argv)
    srv = make_server(args.host, args.port)
    print(f"CallGuard dashboard on http://{args.host}:{srv.server_address[1]}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
