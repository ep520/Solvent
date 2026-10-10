"""Faithful WAV transcription with hash caching and tracked exports.

Whisper provides timestamped speech segments but not diarization.  This module
therefore deliberately marks every turn/role as unknown instead of guessing or
alternating speakers: a made-up speaker assignment would be less trustworthy
than an explicit unknown value.
"""
import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from callguard import models
from callguard import storage

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "asr"
EXPORT = ROOT / "data" / "Transcriptions"
FORMAT_VERSION = "whisper-verbatim-1"
VERBATIM_CONSTRAINT = (
    "Transcribe verbatim in the spoken language and dialect. Do not translate, "
    "paraphrase, summarize, correct, reorder, omit, or substitute words. "
    "Preserve negations, names, numbers, and repetitions exactly as heard."
)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_path(audio_path, profile):
    source_hash = sha256(audio_path)
    payload = {"source_sha256": source_hash, "profile": profile["name"], "model": profile["model"],
               "language": "de", "format": FORMAT_VERSION, "constraint": VERBATIM_CONSTRAINT}
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return CACHE / f"{key}.json", source_hash


def _annotate(raw_segments):
    """Add stable turn and role fields without inventing diarization."""
    segments = []
    last_end = 0.0
    for index, raw in enumerate(raw_segments, 1):
        start, end = raw.get("start"), raw.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end < start or start < last_end:
            raise ValueError(f"invalid non-monotonic timestamp in segment {raw.get('id', index)!r}")
        text = raw.get("text", "").strip()
        if not text:
            continue
        last_end = end
        segments.append({"id": f"s{index:03d}", "turn": f"TURN_{index:03d}",
                         "role": "UNKNOWN_ROLE", "speaker": "UNKNOWN_SPEAKER",
                         "start": round(float(start), 3), "end": round(float(end), 3), "text": text,
                         **{k: raw[k] for k in ("avg_logprob", "no_speech_prob", "compression_ratio") if k in raw}})
    return segments


def transcribe(audio_path, profile=None, force=False):
    """Return a cached, constrained transcription. Cache keys are content hashes, never filenames."""
    audio_path = Path(audio_path)
    p = models.check("asr", profile)
    path, source_hash = cache_path(audio_path, p)
    if path.exists() and not force:
        return json.loads(path.read_text(encoding="utf-8")), True
    raw = models.transcribe(audio_path, language="de", prompt=VERBATIM_CONSTRAINT, profile=p["name"])
    result = {"format": FORMAT_VERSION, "source_file": audio_path.name, "source_sha256": source_hash,
              "asr_profile": p["name"], "asr_model": p["model"], "language_hint": "de",
              "verbatim_constraint": VERBATIM_CONSTRAINT,
              "role_policy": "Whisper has no diarization; roles are explicitly unknown rather than guessed.",
              "segments": _annotate(raw)}
    storage.atomic_write_json(path, result, indent=2)
    return result, False


def _timestamp(seconds):
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def export(result, output_dir=EXPORT):
    """Create tracked JSON and readable Markdown exports with English metadata."""
    output_dir = Path(output_dir)
    stem = Path(result["source_file"]).stem
    json_path, md_path = output_dir / f"{stem}.json", output_dir / f"{stem}.md"
    storage.atomic_write_json(json_path, result, indent=2)
    lines = [f"# {stem}", "", "- ASR model: `whisper-1`", "- Language hint: `de`", "- Role policy: `UNKNOWN_ROLE` is retained where diarization is unavailable; no speaker is guessed.",
             "- Verbatim constraint: no translation, paraphrasing, reordering, correction, or word substitution.", "",
             "| Timestamp | Turn | Role | Speaker | Transcript |", "| --- | --- | --- | --- | --- |"]
    for seg in result["segments"]:
        stamp = f"{_timestamp(seg['start'])} → {_timestamp(seg['end'])}"
        text = seg["text"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {stamp} | {seg['turn']} | {seg['role']} | {seg['speaker']} | {text} |")
    storage.atomic_write_text(md_path, "\n".join(lines) + "\n")
    return json_path, md_path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="callguard.asr")
    ap.add_argument("path", nargs="?", default=ROOT / "data" / "Audio", help="a WAV file or directory")
    ap.add_argument("--profile", help="ASR profile from config/models.json")
    ap.add_argument("--output-dir", default=EXPORT)
    ap.add_argument("--workers", type=int, default=1, help="parallel API requests (default: 1)")
    ap.add_argument("--force", action="store_true", help="ignore the hash cache")
    args = ap.parse_args(argv)
    target = Path(args.path)
    paths = [target] if target.is_file() else sorted(target.glob("*.wav"))
    if not paths:
        sys.exit("no WAV files found")
    models.check("asr", args.profile)

    def one(path):
        result, cached = transcribe(path, profile=args.profile, force=args.force)
        out = export(result, args.output_dir)
        return path, cached, out, len(result["segments"])

    failures = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        jobs = {pool.submit(one, path): path for path in paths}
        for job in as_completed(jobs):
            path = jobs[job]
            try:
                _, cached, out, count = job.result()
                print(f"{'cached' if cached else 'saved '} {path.name}: {count} turns → {out[0].relative_to(ROOT)}")
            except Exception as error:  # Continue so a transient API fault does not lose the full batch.
                failures.append((path, error))
                print(f"FAILED {path.name}: {error}", file=sys.stderr)
    if failures:
        sys.exit(f"{len(failures)}/{len(paths)} WAV files failed; rerun the same command to resume from cache")
    print(f"completed {len(paths)} WAV files; tracked exports in {Path(args.output_dir).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
