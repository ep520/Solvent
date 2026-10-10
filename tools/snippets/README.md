# tools/snippets: measure the snippet selection

The snippet engine lives in [`callguard/snippets.py`](../../callguard/snippets.py) and decides what the
extraction model reads ([`config/snippets.json`](../../config/snippets.json)). The decisive context is often
far from the trigger (C05: T006 vs T032; D09: T004 vs T012); the cue linking attaches it. These tools check
how often the official evidence turns survive the selection:

1. **Measure how much a prefilter would lose** (snippet recall vs the official "Belege", with a random baseline):
   ```
   python3 tools/snippets/eval_snippets.py data off                 # general cues (default, no overfitting)
   python3 tools/snippets/eval_snippets.py data off cues_tuned.json # tuned cues: higher, but overfits the test set
   ```
2. **Show a reviewer the passages around keyword hits** for one call:
   ```
   python3 -m callguard.snippets data/Transkript/Stufe2_C05.txt --keywords data/Stichwortliste.json --semantic off
   ```

`config/snippets/cues_general.json` is the frozen default; `cues_tuned.json` was tuned on the test set and is reported only to
show the gap. `expected.py` parses `Skript_mit_Sollbewertung` and is evaluation-only; it never feeds the pipeline.
