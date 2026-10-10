# tools/snippets: keyword + cue snippet extractor (analysis tool)

Not part of the decision path. CallGuard's extractor reads the **whole call** on purpose: in the data the
decisive context is often far from the trigger (C05: T006 vs T032; D09: T004 vs T012), so a keyword
prefilter would lose recall. This tool is kept for two uses:

1. **Measure how much a prefilter would lose** (snippet recall vs the official "Belege", with a random baseline):
   ```
   pip install rapidfuzz
   python3 tools/snippets/eval_snippets.py data off                 # general cues (default, no overfitting)
   python3 tools/snippets/eval_snippets.py data off cues.json       # tuned cues: higher, but overfits the test set
   ```
2. **Show a reviewer the passages around keyword hits** for one call:
   ```
   python3 tools/snippets/snippets.py data/Transkript/Stufe2_C05.txt --keywords data/Stichwortliste.json
   ```

`cues_general.json` is the frozen default; `cues.json` was tuned on the test set and is reported only to
show the gap. `expected.py` parses `Skript_mit_Sollbewertung` and is evaluation-only; it never feeds the pipeline.
