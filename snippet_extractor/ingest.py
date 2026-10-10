"""
Watched-folder ingest: simulates the bank's call recorder.
Drop a .wav into calls/inbox/ and it is processed automatically.
Only the audio is used: no file names, scripts or expected assessments (README rule).

    pip install watchdog
    python ingest.py --dry-run     # test the folder flow now (no transcription, no LLM)
    python ingest.py               # full pipeline: transcribe -> snippets -> LLM checks

Folders (created on start):
    calls/inbox/       new calls land here
    calls/processing/  being worked on (one at a time, GPU-friendly)
    calls/done/        finished audio
    calls/failed/      errors, with a .err file next to the audio
    results/           one JSON per call, read by the dashboard
"""
import json
import logging
import queue
import shutil
import threading
import time
import sys
import traceback
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver  # works on Docker/network mounts too

ROOT = Path("calls")
INBOX, PROCESSING, DONE, FAILED = (ROOT / d for d in ("inbox", "processing", "done", "failed"))
RESULTS = Path("results")
AUDIO_EXT = {".wav"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ingest")
jobs: "queue.Queue[Path]" = queue.Queue()
queued: set[str] = set()


KEYWORDS = "Builder_70/Stichwortliste.json"   # the compliance officer's keyword list
CHECKS = "checks.json"                       # check definitions for the LLM
SEMANTIC = "bge-m3"                          # "tfidf" or "off" if no GPU
DRY_RUN = "--dry-run" in sys.argv


def transcribe(audio: Path) -> list:
    """Whisper + speaker roles -> list of snippets.Turn. Not built yet."""
    raise NotImplementedError("transcribe() is not implemented yet - run with --dry-run to test the folders")


def process_call(audio: Path) -> dict:
    if DRY_RUN:
        time.sleep(1)
        return {"call_id": audio.stem, "verdict": "no_alert", "hits": [], "dry_run": True}

    import llm_checks as L
    import snippets as S

    turns = transcribe(audio)
    ex = S.extract(turns, KEYWORDS, CHECKS, SEMANTIC)
    hits = L.LLMChecks(L.load_checks(CHECKS), L.LLM(), L.Cache()).run(turns, S.to_llm_candidates(ex["snippets"]))
    return {"call_id": audio.stem,
            "hits": hits,
            "snippet_stats": ex["stats"],
            "transcript": [{"tid": t.tid, "speaker": t.speaker, "start": t.start, "end": t.end, "text": t.text}
                           for t in turns]}


def wait_until_stable(path: Path, checks: int = 3, interval: float = 1.0) -> bool:
    """A file copy fires 'created' before it's finished. Wait until the size stops changing."""
    last, stable = -1, 0
    while stable < checks:
        if not path.exists():
            return False
        size = path.stat().st_size
        stable = stable + 1 if size == last and size > 0 else 0
        last = size
        time.sleep(interval)
    return True


def enqueue(path: Path) -> None:
    if path.suffix.lower() in AUDIO_EXT and path.parent == INBOX and path.name not in queued:
        queued.add(path.name)
        jobs.put(path)
        log.info("queued   %s", path.name)


def worker() -> None:
    while True:
        src = jobs.get()
        try:
            if not wait_until_stable(src):
                continue
            work = PROCESSING / src.name
            shutil.move(src, work)
            log.info("started  %s", src.name)
            t0 = time.time()
            result = process_call(work)
            result["processed_seconds"] = round(time.time() - t0, 1)
            (RESULTS / f"{work.stem}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
            shutil.move(work, DONE / src.name)
            log.info("finished %s -> %s (%.1fs)", src.name, result.get("verdict"), result["processed_seconds"])
        except Exception:
            log.exception("failed   %s", src.name)
            bad = PROCESSING / src.name
            if bad.exists():
                shutil.move(bad, FAILED / src.name)
            (FAILED / f"{src.stem}.err").write_text(traceback.format_exc())
        finally:
            queued.discard(src.name)
            jobs.task_done()


class InboxHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            enqueue(Path(event.src_path))

    def on_moved(self, event):  # e.g. `mv file.wav calls/inbox/`
        if not event.is_directory:
            enqueue(Path(event.dest_path))


def main() -> None:
    for d in (INBOX, PROCESSING, DONE, FAILED, RESULTS):
        d.mkdir(parents=True, exist_ok=True)

    # Recover anything left mid-processing by a crash, then pick up files already waiting.
    for f in PROCESSING.iterdir():
        shutil.move(f, INBOX / f.name)
    for f in sorted(INBOX.iterdir()):
        enqueue(f)

    threading.Thread(target=worker, daemon=True).start()
    observer = PollingObserver(timeout=1)
    observer.schedule(InboxHandler(), str(INBOX), recursive=False)
    observer.start()
    log.info("watching %s/ - drop .wav files in to process them%s", INBOX, " (dry run)" if DRY_RUN else "")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
