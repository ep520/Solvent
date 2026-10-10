"""Compare ASR profiles on the recordings: word error rate and keyword survival against the scripts.

  python3 tools/asr_wer.py                         # every ASR profile that has cached transcripts
  python3 tools/asr_wer.py faster_whisper openai   # just these (openai = the earlier whisper-1 exports)

Reads only cached transcripts (cache/asr), never runs a model. The reference is the spoken script in
data/Transkript (evaluation only, never a pipeline input). The scripts are Swiss German; an ASR that writes
Standard German gets a high WER even when it understood everything, so "keywords kept" is the number that
matters for detection: of the official keyword phrases (de + gsw) present in the script, how many are still
found in the transcript.
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from callguard import asr, models  # noqa: E402
from callguard import extract as ex  # noqa: E402
from callguard.pipeline import script_name  # noqa: E402


def words(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.findall(r"\w+", text)


def wer(ref, hyp):
    """Word-level Levenshtein distance / reference length."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / max(1, len(ref))


def phrases(keywords):
    out = set()
    for kw in keywords["keywords"]:
        if kw.get("enabled", True):
            out |= {" ".join(words(p)) for p in kw.get("de", []) + kw.get("gsw", []) if words(p)}
    return out


def main(profiles):
    config = models.load_config()
    if not profiles:
        profiles = [n for n, p in config["profiles"].items() if p.get("asr_model")]
    keywords = phrases(ex.load_json(ROOT / "data" / "Stichwortliste.json"))
    wavs = sorted((ROOT / "data" / "Audio").glob("*.wav"))
    print(f"{'profile':<16}{'calls':>6}{'WER':>8}{'keywords kept':>16}")
    for name in profiles:
        p = models.resolve("asr", name, config)
        errs, kept, total, n = [], 0, 0, 0
        for wav in wavs:
            path, _ = asr.cache_path(wav, p)
            if not path.exists():                   # fall back to the tracked export, if it came from this profile
                export = ROOT / "data" / "Transcriptions" / f"{wav.stem}.json"
                if export.exists() and json.loads(export.read_text(encoding="utf-8")).get("asr_profile") == name:
                    path = export
            script = ROOT / "data" / "Transkript" / f"{script_name(wav.stem)}.txt"
            if not path.exists() or not script.exists():
                continue
            hyp_text = " ".join(s["text"] for s in json.loads(path.read_text(encoding="utf-8"))["segments"])
            ref_text = " ".join(s["text"] for s in ex.segments_from_transcript(script))
            ref, hyp = words(ref_text), words(hyp_text)
            errs.append(wer(ref, hyp))
            ref_j, hyp_j = " " + " ".join(ref) + " ", " " + " ".join(hyp) + " "
            present = [k for k in keywords if f" {k} " in ref_j]
            total += len(present)
            kept += sum(f" {k} " in hyp_j for k in present)
            n += 1
        if n:
            print(f"{name:<16}{n:>6}{sum(errs) / n:>8.1%}{f'{kept}/{total}':>16}")
        else:
            print(f"{name:<16}{0:>6}{'-':>8}{'no cached transcripts':>22}")


if __name__ == "__main__":
    main(sys.argv[1:])
