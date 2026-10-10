"""Watched-folder ingest: no manual step per call.

Drop a WAV into data/Inbox/ (the bank's call recorder would write there). Each new file is:

  1. moved to data/Audio/                        (the dashboard's audio folder)
  2. transcribed with the ASR profile            (cache/asr + data/Transcriptions export)
  3. extracted + decided with the chat profile   (cache/extract; same code path as `pipeline audio`)
  4. written to runs/ingest/<call>.json          (canonical result, with counterfactuals)
  5. reported to the Trigger API (server.py) when the label is alarm or review, if --trigger-url is set

The dashboard (python3 -m callguard.ui) then shows the call without any further step: it reads the
same transcripts and cached extractions. Only the audio is used as input; the file name is an ID.

  python3 -m callguard.ingest                       # watch data/Inbox every 5 s
  python3 -m callguard.ingest --once                # process what is there, then exit
  python3 -m callguard.ingest --trigger-url http://localhost:8080/triggers --dashboard-url http://localhost:8090
"""
import argparse
import json
import os
import shutil
import sys
import time
import traceback
import urllib.request
from pathlib import Path

from callguard import asr
from callguard import extract as ex
from callguard import models
from callguard import pipeline

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "Inbox"
AUDIO = ROOT / "data" / "Audio"
FAILED = ROOT / "data" / "Inbox" / "failed"
RESULTS = ROOT / "runs" / "ingest"


def stable(path, wait=1.0):
    """A recorder may still be writing: only take files whose size did not change for `wait` seconds."""
    try:
        size = path.stat().st_size
        time.sleep(wait)
        return size > 0 and path.stat().st_size == size
    except FileNotFoundError:
        return False


def free_name(folder, name):
    target = folder / name
    n = 2
    while target.exists():
        target = folder / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return target


def report(result, trigger_url, dashboard_url):
    """POST {url, title, description} to the Trigger API for calls that need a person."""
    event = max(result["events"], key=lambda e: {"no_alert": 0, "review": 1, "alarm": 2}[e["label"]], default=None)
    first = next((r for c in (event or {}).get("conditions", {}).values() for r in c["evidence"]), None)
    at = f"?t={int(first['start'])}" if first and first.get("start") is not None else ""
    body = {"url": f"{dashboard_url.rstrip('/')}/#{result['call']}{at}",
            "title": f"{result['label'].upper()}: {event['title'] if event else 'technical review'} ({result['call']})",
            "description": (event or {}).get("summary", "; ".join(result.get("reasons", [])))[:2000]}
    req = urllib.request.Request(trigger_url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def process(target, policies, keywords, args):
    transcript, _ = asr.transcribe(target)
    asr.export(transcript)                                    # data/Transcriptions/<call>.json, read by the dashboard
    result, _ = pipeline.run_audio(target, policies, keywords, profile=args.profile)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{result['call']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{result['label']:<8} {result['call']}  {','.join(result['reasons'])}", flush=True)
    if args.trigger_url and result["label"] in ("alarm", "review"):
        try:
            report(result, args.trigger_url, args.dashboard_url)
        except Exception as e:  # the decision is saved; a down trigger endpoint must not lose it
            print(f"  trigger not sent: {e}", file=sys.stderr)
    return result


def scan(policies, keywords, args):
    done = 0
    for wav in sorted(INBOX.glob("*.wav")):
        if not stable(wav):
            continue
        AUDIO.mkdir(parents=True, exist_ok=True)
        target = free_name(AUDIO, wav.name)
        shutil.move(str(wav), target)
        try:
            process(target, policies, keywords, args)
            done += 1
        except Exception as e:
            FAILED.mkdir(parents=True, exist_ok=True)
            dest = free_name(FAILED, wav.name)
            shutil.move(str(target), dest)  # out of data/Audio so the dashboard does not list it as pending
            dest.with_suffix(".err").write_text(traceback.format_exc(), encoding="utf-8")
            print(f"FAILED   {wav.name}: {e}", file=sys.stderr, flush=True)
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(prog="callguard.ingest")
    ap.add_argument("--once", action="store_true", help="process the inbox once and exit")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between inbox scans")
    ap.add_argument("--profile", help="chat profile (default: CALLGUARD_CHAT or models.json)")
    ap.add_argument("--asr-profile", help="ASR profile (default: CALLGUARD_ASR or models.json)")
    ap.add_argument("--require-self-hostable", action="store_true")
    ap.add_argument("--trigger-url", help="Trigger API endpoint, e.g. http://localhost:8080/triggers")
    ap.add_argument("--dashboard-url", default="http://localhost:8090")
    ap.add_argument("--policies", default=ROOT / "config" / "policies.json")
    ap.add_argument("--keywords", default=ROOT / "data" / "Stichwortliste.json")
    args = ap.parse_args(argv)
    if args.asr_profile:  # pipeline.run_audio resolves the ASR profile from the environment
        os.environ["CALLGUARD_ASR"] = args.asr_profile
    if args.profile:
        os.environ["CALLGUARD_CHAT"] = args.profile  # the dashboard reads the cache of this profile
    try:
        models.check("chat", args.profile, require_self_hostable=args.require_self_hostable)
        models.check("asr", args.asr_profile, require_self_hostable=args.require_self_hostable)
    except models.ModelError as e:
        sys.exit(f"model not configured: {e}")
    policies, keywords = ex.load_json(args.policies), ex.load_json(args.keywords)
    INBOX.mkdir(parents=True, exist_ok=True)
    print(f"watching {INBOX.relative_to(ROOT)}/ for .wav files (Ctrl+C to stop)", flush=True)
    while True:
        scan(policies, keywords, args)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
