"""
Reads the official expected assessments from Skript_mit_Sollbewertung/*.txt.
For EVALUATION ONLY - never feed these into the pipeline (README rule).

Header fields used:
  Testziel: trade                     -> which check the call tests
  Ereignis: present / absent          -> is the behaviour really there?
  Bearbeitung: alarm / no_alert / review
  Begründung: ...                     -> official reasoning
  Prüfpräfix bis T032; Belege: T006, T032   -> decisive turns (C/D files)
  Aufgabe: ... / Belegstellen laut Skript:  -> task and decisive turns (M files)
"""
import re
from pathlib import Path


def parse_expected(text: str) -> dict:
    def field(name):
        m = re.search(rf"^{name}:\s*(.+)$", text, re.M)
        return m.group(1).strip() if m else None

    belege_line = re.search(r"Belege:\s*([^\n]+)", text)
    prefix = re.search(r"Prüfpräfix bis\s*(T\d+)", text)
    if belege_line:                                     # C/D files: "Belege: T006, T032"
        evidence = re.findall(r"T\d{2,4}", belege_line.group(1))
    else:                                               # M files: indented lines under "Belegstellen"
        block = re.search(r"Belegstellen[^\n]*\n((?:[ \t]+T\d+[^\n]*\n?)+)", text)
        evidence = re.findall(r"^\s+(T\d{2,4})\b", block.group(1), re.M) if block else []
    return {
        "testziel": field("Testziel") or field("Aufgabe"),
        "ereignis": field("Ereignis"),
        "verdict": (field("Bearbeitung") or "").split()[0] if field("Bearbeitung") else None,
        "reason": field("Begründung"),
        "evidence": list(dict.fromkeys(evidence)),
        "decidable_by": prefix.group(1) if prefix else None,
        "keywords": field("Keywordstellen"),
    }


def load_expected(data_dir) -> dict:
    folder = Path(data_dir, "Skript_mit_Sollbewertung")
    return {f.stem: parse_expected(f.read_text(encoding="utf-8")) for f in sorted(folder.glob("*.txt"))}


if __name__ == "__main__":
    import sys
    for stem, e in load_expected(sys.argv[1] if len(sys.argv) > 1 else "data").items():
        print(f"{stem:12} {e['verdict']:9} {e['testziel'] or '-':14} evidence={e['evidence']}")
