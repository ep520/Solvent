"""
Snippet recall against the OFFICIAL expected assessments (Skript_mit_Sollbewertung).

For every call: does each decisive turn ("Belege") land inside some snippet?
Also shows how much of the call is kept. Evaluation only - the pipeline never sees these files.

    python eval_snippets.py data off        # keywords + digits
    python eval_snippets.py data tfidf      # + weak semantic stand-in
    python eval_snippets.py data bge-m3     # + real semantic model (GPU machine)
    python eval_snippets.py data off cues_general.json   # compare another cue file
    python eval_snippets.py data off off                 # no cues at all
"""
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # repo root: the engine lives in callguard/
from callguard import snippets as S  # noqa: E402

try:
    from .expected import load_expected
except ImportError:
    from expected import load_expected


def random_baseline(n_turns, key_idx, kept_frac, trials=200, seed=0):
    """Expected recall if the same share of turns were picked at random (no intelligence)."""
    rng = random.Random(seed)
    k = max(1, round(kept_frac * n_turns))
    hit = sum(sum(i in set(rng.sample(range(n_turns), k)) for i in key_idx) for _ in range(trials))
    return hit / trials


POLICIES = str(Path(__file__).resolve().parents[2] / "config" / "policies.json")


def run(data_dir="data", semantic="off", checks=POLICIES, cues="cues_general.json"):
    kws = S.load_keywords(f"{data_dir}/Stichwortliste.json")
    emb = S.Embedder(semantic) if semantic != "off" else None
    expected = load_expected(data_dir)
    if not expected:
        print(f"No files in {data_dir}/Skript_mit_Sollbewertung - nothing to compare against.")
    found = total = 0
    base = 0.0
    by_verdict = defaultdict(lambda: [0, 0])
    pct = []
    for f in sorted(Path(data_dir, "Transkript").glob("*.txt")):
        r = S.extract(str(f), kws, checks, semantic, embedder=emb, cues=None if cues == "off" else cues)
        covered = {lab for s in r["snippets"] for lab in s["turn_labels"]}
        exp = expected.get(f.stem, {})
        key = exp.get("evidence", [])
        miss = [k for k in key if k not in covered]
        labels = [t.tid for t in r["turns"]]
        kept_frac = r["stats"]["kept_turns"] / max(1, r["stats"]["turns"])
        base += random_baseline(len(labels), [labels.index(k) for k in key if k in labels], kept_frac)
        found += len(key) - len(miss)
        total += len(key)
        v = exp.get("verdict") or "?"
        by_verdict[v][0] += len(key) - len(miss)
        by_verdict[v][1] += len(key)
        pct.append(r["stats"]["kept_text_pct"])
        fams = sorted({h["family"] + ("#" if h["how"] == "digits" else "") for s in r["snippets"] for h in s["hits"]})
        print(f"{f.stem:12} {v:9} {exp.get('testziel') or '-':14} snippets={r['stats']['snippets']}  "
              f"kept={r['stats']['kept_text_pct']:5.1f}%  evidence={key or '-'}  miss={miss or '-'}  hits={fams}")
    print(f"\nevidence recall {found}/{total}" + (f" = {100 * found / total:.0f}%" if total else ""))
    if total:
        print(f"random baseline  {base:.1f}/{total} = {100 * base / total:.0f}%   "
              f"(same amount of text, turns picked at random)")
    for v, (a, b) in sorted(by_verdict.items()):
        print(f"  {v:9} {a}/{b}")
    print(f"mean text kept {sum(pct) / max(1, len(pct)):.1f}%")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "data", sys.argv[2] if len(sys.argv) > 2 else "off",
        cues=sys.argv[3] if len(sys.argv) > 3 else "cues_general.json")
