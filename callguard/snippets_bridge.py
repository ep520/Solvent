"""Opt-in bridge to tools/snippets/snippets.py: send only the turns worth judging to the chat
model, instead of the whole call. OFF BY DEFAULT (pipeline.py's --snippets flag); the normal path
(whole transcript) is unchanged and remains what every existing cache entry, test and fixture uses.

Why opt-in, not the default: the dataset's Stufe2/Stufe3 cases are deliberately keyword-free (PLAN.md),
so an upstream filter risks dropping exactly the cases the challenge is testing. The semantic finder
(a multilingual embedding model, BAAI/bge-m3 by default) exists precisely to catch those, but that is
an empirical question -- measure it on the 21 known cases (`eval --snippets --smoke`) before trusting
it on anything else.

Nothing here imports tools/snippets at module load time: that package needs `rapidfuzz` (always) and,
only for the semantic finder, `sentence-transformers` plus a ~2 GB model download on first use. Neither
is a dependency of the core pipeline, so importing this bridge (or running the pipeline without
--snippets) must never require them. Only calling select_snippets() does, and only for the parts you
actually ask for: semantic="off" (default here) needs neither.
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNIPPETS_DIR = ROOT / "tools" / "snippets"


class SnippetsUnavailable(Exception):
    """tools/snippets/snippets.py or one of its dependencies could not be imported."""


def _snippets_module():
    if str(SNIPPETS_DIR) not in sys.path:
        sys.path.insert(0, str(SNIPPETS_DIR))
    try:
        import snippets  # tools/snippets/snippets.py
    except ImportError as e:
        raise SnippetsUnavailable(
            f"--snippets needs the tools/snippets dependencies (pip install rapidfuzz; add "
            f"sentence-transformers only for semantic=bge-m3): {e}") from e
    return snippets


def _quality(segment):
    lp = segment.get("avg_logprob")
    return math.exp(lp) if lp is not None else None


def _to_turn(Turn, segment, idx):
    return Turn(idx=idx, speaker=(segment.get("speaker") or "unknown"), text=segment["text"],
               start=segment.get("start"), end=segment.get("end"), tid=segment["id"],
               quality=_quality(segment))


def select_snippets(segments, keywords_path, semantic="off", checks=None, cues=None, fuzzy=True):
    """Keep only the segments a snippet (keyword / digit / cue / optional semantic hit, plus their
    configured context) actually covers, in original order and the same {id, start, end, text,
    speaker, avg_logprob} shape as `segments` -- so build_messages()/build_schema() need no changes.

    keywords_path: path to Stichwortliste.json -- snippets.load_keywords() reads and parses the file
              itself (Inventx families/de/gsw format), so this takes the path, not extract.load_json's
              already-parsed dict; they are two independent readers of the same file.
    semantic: "off" (default, no download) | "tfidf" (no download, weaker) | "bge-m3" (downloads
              BAAI/bge-m3 via sentence-transformers the first time it runs). Has no effect unless
              `checks` is also given: both are required to turn the semantic finder on.
    cues: path to a cues JSON (e.g. tools/snippets/cues_general.json), or None to skip that finder.

    Returns (kept_segments, report): report is snippets.extract()'s own {"stats", "snippets", ...},
    kept purely for display/debugging (e.g. pipeline.py can print report["stats"]).
    """
    snippets = _snippets_module()
    Turn = snippets.Turn
    turns = [_to_turn(Turn, seg, i) for i, seg in enumerate(segments)]
    report = snippets.extract(turns, str(keywords_path), checks=checks, semantic=semantic, fuzzy=fuzzy,
                              cues=str(cues) if cues else None)
    kept_idx = {i for s in report["snippets"] for i in s["turn_ids"]}
    kept_segments = [seg for i, seg in enumerate(segments) if i in kept_idx]
    return kept_segments, report
