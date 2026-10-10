"""CLI: run the text pipeline on transcripts and evaluate it against the expected assessments.

  python3 -m callguard.pipeline text data/Transkript/Stufe1_D02.txt [--no-speakers]
  python3 -m callguard.pipeline eval [--no-speakers] [--only C09 | --smoke] [--profile private]
  python3 -m callguard.pipeline audio data/Audio/Stufe1_D02-K1.wav
  python3 -m callguard.pipeline eval --audio [--only C09 | --smoke]
  python3 -m callguard.pipeline freeze [--no-speakers]   # cached extractions -> tests/fixtures/replay
"""
import argparse
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from callguard import asr
from callguard import decide as dec
from callguard import extract as ex
from callguard import models

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LABELS = ("alarm", "review", "no_alert")
SMOKE = ("Stufe1_D02", "Stufe1_D03", "Stufe2_C09", "Stufe2_C10", "Stufe3_C17")


def script_name(call):
    """Stufe1_D02-K2 -> Stufe1_D02 (audio file to its dialogue); used only to look up the expected assessment."""
    return re.sub(r"-K\d$", "", call)


def audio_segments(wav, profile=None):
    """Whisper segments for a WAV (hash-cached by callguard.asr); Whisper has no speakers, so none are shown."""
    transcript, _ = asr.transcribe(wav, profile=profile)
    return [{**s, "speaker": None} for s in transcript["segments"]]


def run_audio(wav, policies, keywords, profile=None, threshold=None, **_):
    return run_segments(Path(wav).stem, str(Path(wav)), audio_segments(wav), policies, keywords, profile, threshold,
                        speakers=False, audio=True)


def run_text(path, policies, keywords, speakers=True, profile=None, threshold=None):
    segments = ex.segments_from_transcript(path, speakers=speakers)
    return run_segments(Path(path).stem, str(Path(path)), segments, policies, keywords, profile, threshold,
                        speakers=speakers, audio=False)


def run_segments(call, source, segments, policies, keywords, profile, threshold, speakers, audio):
    try:
        extraction, error = ex.extract(segments, policies, profile=profile), None
    except models.ModelError as e:
        extraction, error = {}, e
    result = dec.decide(extraction, segments, policies, threshold=threshold, error=error)
    result.update(call=call, source=source, speakers=speakers, audio=audio,
                  keyword_hits=ex.keyword_hits(segments, keywords))
    return result, segments


def cited_segments(result):
    refs = [r for e in result["events"] for c in e["conditions"].values() for r in c["evidence"]]
    refs += [r for e in result["events"] for r in e["evidence"]]
    return {sid for r in refs for sid in r["segment_ids"]}


def gold(call):
    """Expected assessment from Skript_mit_Sollbewertung; used only for scoring, never as input."""
    text = (DATA / "Skript_mit_Sollbewertung" / f"{call}.txt").read_text(encoding="utf-8")
    field = lambda name: re.search(rf"^{name}: (.+)$", text, re.M).group(1).strip()
    return {"label": field("Bearbeitung"), "family": field("Testziel"), "event": field("Ereignis"),
            "evidence": re.findall(r"T\d{3}", field("Prüfpräfix bis T\\d{3}; Belege"))}


def metrics(rows):
    confusion = {g: {p: 0 for p in LABELS} for g in LABELS}
    for r in rows:
        confusion[r["gold"]][r["pred"]] += 1
    n_alarm = sum(r["gold"] == "alarm" for r in rows)
    n_not_alarm = len(rows) - n_alarm
    pred_alarm = sum(r["pred"] == "alarm" for r in rows)
    true_alarm = sum(r["pred"] == "alarm" and r["gold"] == "alarm" for r in rows)
    false_alarms = sum(r["pred"] == "alarm" and r["gold"] != "alarm" for r in rows)
    missed_strict = sum(r["gold"] == "alarm" and r["pred"] != "alarm" for r in rows)
    missed_lenient = sum(r["gold"] == "alarm" and r["pred"] == "no_alert" for r in rows)
    flagged = [r for r in rows if r["pred"] != "no_alert" and r["gold"] != "no_alert" and r["evidence_hit"] is not None]
    return {
        "calls": len(rows),
        "extraction_errors (model unavailable, not a classification)": sum(r.get("extraction_error", False) for r in rows),
        "accuracy": round(sum(r["gold"] == r["pred"] for r in rows) / len(rows), 3),
        "false_alarms": f"{false_alarms}/{n_not_alarm}",
        "missed_strict (alarm expected, not alarm)": f"{missed_strict}/{n_alarm}",
        "missed_lenient (alarm expected, no_alert)": f"{missed_lenient}/{n_alarm}",
        "alarm_precision": f"{true_alarm}/{pred_alarm}",
        "alarm_recall": f"{true_alarm}/{n_alarm}",
        "review_to_alarm (unjustified confident escalation)": confusion["review"]["alarm"],
        "review_to_no_alert (uncertainty lost)": confusion["review"]["no_alert"],
        "review_rate": f"{sum(r['pred'] == 'review' for r in rows)}/{len(rows)}",
        "evidence_hit (cited an expected passage)": f"{sum(r['evidence_hit'] for r in flagged)}/{len(flagged)}" if flagged else "n/a (audio segments are not script turns)",
        "confusion (gold -> pred)": confusion,
        "by_level": {lvl: f"{sum(r['gold'] == r['pred'] for r in rows if r['call'].startswith(lvl))}/"
                          f"{sum(r['call'].startswith(lvl) for r in rows)}" for lvl in ("Stufe1", "Stufe2", "Stufe3")},
        **({"by_audio": {name: f"{sum(r['gold'] == r['pred'] for r in rows if r['call'][-2:] in ks)}/"
                               f"{sum(r['call'][-2:] in ks for r in rows)}"
                         for name, ks in (("clean (K1/K3)", ("K1", "K3")), ("noisy (K2/K4)", ("K2", "K4")))}}
           if any(r["call"][-3:-1] == "-K" for r in rows) else {}),
    }


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
    ap.add_argument("--workers", type=int, default=1, help="calls evaluated in parallel")
    ap.add_argument("--policies", default=ROOT / "config" / "policies.json")
    ap.add_argument("--keywords", default=DATA / "Stichwortliste.json")
    args = ap.parse_args(argv)
    policies, keywords = ex.load_json(args.policies), ex.load_json(args.keywords)
    try:
        models.check("chat", args.profile)
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
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda p: run(p, policies, keywords, **opts)[0], paths))
    mode = "audio" if args.audio else "nospeakers" if args.no_speakers else "speakers"
    out = ROOT / "runs" / f"eval-{time.strftime('%Y%m%d-%H%M%S')}-{mode}"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for result in results:
        g = gold(script_name(result["call"]))
        rows.append({"call": result["call"], "gold": g["label"], "pred": result["label"], "reasons": result["reasons"],
                     "extraction_error": bool(result.get("error")),
                     "evidence_hit": None if args.audio else bool(cited_segments(result) & set(g["evidence"]))})
        (out / f"{result['call']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        mark = "ok  " if g["label"] == result["label"] else "ERR " if result.get("error") else "MISS"
        print(f"{mark} {result['call']:<12} gold={g['label']:<8} pred={result['label']:<8} {','.join(result['reasons'])}")
    m = metrics(rows)
    print(json.dumps(m, indent=1))
    (out / "summary.json").write_text(json.dumps({"args": {k: str(v) for k, v in vars(args).items()}, "metrics": m,
                                                  "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved {out.relative_to(ROOT)}/ (one canonical JSON per call + summary.json)")


if __name__ == "__main__":
    main()
