# CallGuard pipeline

How a Swiss German bank call becomes an **alarm**, **review** or **no alert**, what is implemented and
verified, and what is still open. For the step-by-step work log and decisions history see
[`PLAN.md`](PLAN.md); for the case brief see [`README.md`](README.md).

## 1. Overview

```
data/Audio/*.wav (mono, 16 kHz, Swiss German)
   │
   ▼  callguard/asr.py ─ Whisper (whisper-1) via the model router, cached by file content hash
segments [{id, start, end, text, avg_logprob}]          → data/Transcriptions/*.json|.md
   │
   ├─ extract.keyword_hits ─ Stichwortliste.json phrases → highlights and counts only
   │
   ▼  callguard/extract.py ─ ONE structured LLM request per call (Claude Sonnet 5), cached
facts: {"families": {<family>: {"events": [{conditions: {<predicate>: {state, evidence}}}]}}}
   │      state ∈ true | false | unknown, evidence = segment id + short quote
   ▼  callguard/decide.py ─ pure, deterministic code
   1. anchor every quote in the transcript (exact, then bounded approximate alignment)
   2. three-valued rules per family
   3. ASR-quality escalation threshold
   4. call label = max(alarm > review > no_alert), reason codes, template explanation
   ▼
one canonical JSON per call → metrics (pipeline.py) · dashboard (ui.py) · ±10 s clips (evidence.py)
```

Design principle: **the language model only reports facts with quotations; every decision is made by
code**. The same facts and configuration always give the same verdict, and every verdict can be traced
to a rule, the state of each condition, and the exact words in the recording.

## 2. Stages

### 2.1 Transcription (`callguard/asr.py`)

- `whisper-1` through the OpenAI API (`.env` holds `OPENAI_API_KEY`, never committed), language hint
  `de`, a "transcribe verbatim" prompt, `verbose_json` segments with timestamps and `avg_logprob`.
- Cache key = SHA-256 of the WAV content + profile + model + format, never the file name. Each WAV is
  transcribed once (a full pass over the 42 WAVs costs about USD 1.15).
- Whisper has no diarization: speakers and roles are stored as explicitly unknown, never guessed.
- No audio preprocessing: the files are already mono 16 kHz and under the 25 MB API limit.

### 2.2 Fact extraction (`callguard/extract.py`, prompt version `extract-4`)

- One request per call with the **whole** transcript, every time. There is no keyword pre-filter,
  because Level 2 and 3 calls contain no listed keyword at all.
- The JSON schema requires every family; `"events": []` means "assessed, nothing found", so a missing
  family exposes an incomplete answer.
- Prompt rules that matter: `true` only when a passage establishes the condition, `false` only when a
  passage explicitly excludes it, otherwise `unknown` (including "I don't know" and "not proven"); an
  ambiguous object makes dependent conditions `unknown`; a later refusal neither undoes a request nor
  proves one; quotes are 3–10 consecutive words copied from one segment.
- Families and predicates live in [`config/policies.json`](config/policies.json) (`policies-0.4`):

| Family | Alarm requires all of |
|---|---|
| trade | `own_trade_request`, `information_nonpublic`, `information_market_relevant`, `request_based_on_information` |
| access | `value_spoken`, `access_secret`, `secret_active` |
| disclosure | `advisor_disclosed`, `third_party_detail`, `authority_absent` |
| documentation | `concealment_requested`, `fact_relevant`, `designated_recipient` |
| splitting | `split_requested`, `movements_connected`, `evasion_purpose` |
| numbers | none: classifies the number (card, masked digits, invoice, reservation, amount…), **never an alarm** |

### 2.3 Decision (`callguard/decide.py`)

Two deterministic techniques only.

**1. Evidence anchoring.** Each quote must be found in the cited segment, or in that segment joined
with one neighbour:
- exact match (case and punctuation ignored);
- otherwise a word-level approximate alignment (`difflib`) with similarity ≥ `grounding.min_similarity`
  (0.92) **and identical negations** (nöd, nicht, kei, nie…). Measured on real failures: copy slips of
  the model score ≥ 0.943, quotes with a changed meaning ≤ 0.864 (a flipped negation scored exactly
  0.864, which the negation check rejects);
- the evidence shown is always the **real transcript words**, with `match` = similarity;
- cited ids are normalised (`s17` → `s017`);
- a `true` or `false` without anchored evidence becomes `unknown`.

**2. Three-valued rules.** Per event: any `false` → absent (no alert); else any `unknown` → undetermined
(review); else present. A present event becomes an **alarm** only if every decisive condition has at
least one quote whose ASR quality `exp(avg_logprob)` reaches the threshold.

Review always shows its reason:

| Reason | Meaning |
|---|---|
| `missing_policy_fact` | The call does not establish a required fact; the open question is shown |
| `technical_uncertainty` | Extraction failed or was incomplete, or evidence could not be anchored |
| `below_escalation_threshold` | All conditions supported, but a decisive passage was transcribed with low quality |

**Threshold.** `escalation.presets` = sensitive 0.5, balanced 0.6 (default), conservative 0.75. It is an
ASR-quality heuristic, not a fraud probability. When no score exists (scripts), quality is reported as
unavailable and nothing is downgraded.

### 2.4 Outputs

- `python3 -m callguard.pipeline eval [--audio]` writes `runs/eval-<timestamp>-<mode>/<call>.json`
  (canonical result: label, reasons, events, conditions, quotes with times and quality, keyword hits,
  model and prompt versions) plus `summary.json` with the metrics.
- `callguard/evidence.py` cuts ±10 s clips from the **original** WAV (standard library `wave`).
- `callguard/ui.py` serves the dashboard and a read-only API over the cached results (see §4).

### 2.5 Model router (`callguard/models.py`, `config/models.json`)

Standard library only, two frozen adapters: `claude_cli` (`claude -p --model claude-sonnet-5` with a
JSON schema, no tools, no user settings) and `openai_compatible` (OpenAI, vLLM, Ollama, llama.cpp,
speaches, private gateways). Switching model is a profile change (`CALLGUARD_CHAT`, `CALLGUARD_ASR` or
`config/models.json`); no code changes. Any switch must pass `eval --smoke` before a full evaluation.

## 3. Results (2026-10-10)

| Input | Calls | Correct | False alarms | Alarms found | Notes |
|---|---|---|---|---|---|
| **Audio** (Whisper → Sonnet 5), clean K1/K3 | 21 | **21/21** | 0 | 8/8 | |
| **Audio**, noisy K2/K4 | 21 | **21/21** | 0 | 8/8 | |
| Scripts, with speaker labels | 21 | 21/21 | 0 | 8/8 | regression check |
| Scripts, without speaker labels | 21 | 21/21 | 0 | 8/8 | regression check |

Audio details: 0 review→alarm, 0 review→no-alert, 6/42 reviews (C17, C19, C20 × 2, all
`missing_policy_fact`), 0 extraction errors. Run folders and the full history are in `PLAN.md` §8.

Threshold sensitivity on the 42 audio calls: sensitive and balanced give identical results; conservative
moves one call (`Stufe1_D02-K2`, noisy, quality 0.72) from alarm to review.

## 4. How to run

```bash
cp .env.example .env                                   # then set OPENAI_API_KEY (only needed for new audio)
python3 -m unittest discover -s tests                  # 69 tests, no network, no keys
python3 -m callguard.models                            # active chat / ASR profiles
python3 -m callguard.asr data/Audio --workers 4        # transcribe (cached; skips known files)
python3 -m callguard.pipeline audio data/Audio/Stufe1_D02-K1.wav   # one call, text explanation
python3 -m callguard.pipeline eval --audio --workers 6 # all 42 WAVs with metrics
python3 -m callguard.pipeline eval --smoke --workers 5 # 5-call regression set (scripts)
python3 -m callguard.ui                                # dashboard on http://127.0.0.1:8090
```

The dashboard lists all analysed calls with assessment, policy conditions, supporting passages, the
original audio (jump buttons start 10 s before each passage), downloadable clips, the threshold presets
and keyword highlights (highlights never change a classification). It never calls a model; calls
without a cached extraction are listed as pending.

## 5. Tests (69)

| Suite | What it guards |
|---|---|
| `test_decide.py` | rules, anchoring (exact, neighbour window, approximate alignment, negation guard, id normalisation), threshold, reasons, synthetic outcomes missing from the dataset (trade→review, disclosure→review, documentation→no alert); keyword matcher reproduces every annotated keyword hit of the 21 scripts |
| `test_policies.py` | hand-written **oracle** extractions for all 21 scripts must give the expected assessment and cite an expected turn; **replay** of 42 real Sonnet extractions (`extract-1`) through the current rules; config/fixture consistency |
| `test_models.py` | router with a fake HTTP server and a fake `claude` command, `.env` loading, retries |
| `test_asr.py` | segment annotation, exports, timestamp validation |
| `test_ui.py` | clips, mapping to dashboard fields per threshold, server routes, Range requests, path safety |

Mutation checks confirmed the policy tests fail when a condition is removed, when "undetermined" is
treated as no alert, or when the threshold is broken.

## 6. What is implemented and verified

- [x] Transcription of all 42 calls, hash-cached, exported and checked (complete coverage, matching
      hashes, no prompt leakage).
- [x] Five fraud checks plus number classification, as configuration, with one extraction per call.
- [x] Deterministic decisions with explicit reasons and open questions; uncertainty goes to review.
- [x] Evidence anchored to the real transcript, with timestamps, quality and ±10 s audio clips.
- [x] Adjustable threshold (three presets, editable without code) with a visible effect.
- [x] Metrics on false alarms and missed cases, split by level and by clean/noisy audio.
- [x] Review dashboard connected to the pipeline, tested in a browser.
- [x] No manual step per call: audio in, canonical result and clips out.

## 7. What is missing or still to evaluate

**To do**
- [ ] Manual audit of the decisive evidence of the demo calls and of a sample of alarm/review clips:
      each decisive condition must be supported by a passage that really establishes it. `evidence_hit`
      and the anchoring only show that a quote exists, not that it was understood correctly.
- [ ] Release run: final `eval` on scripts and audio, one `freeze` of the extractions
      (`tests/fixtures/replay/claude-sonnet-5/extract-4/`), frozen configuration.
- [ ] Demo preparation: one call from audio to alarm with clips, a threshold or keyword change with a
      visible effect, a borderline call (D03: keywords present, no alert), the metrics.
- [ ] Hidden test set (30 % of the material, run on Sunday): transcribe, `eval --audio`, report.
- [ ] Keyword editing in the dashboard only toggles highlights; persisting changes to
      `Stichwortliste.json` from the UI is not implemented.
- [ ] Optional: Trigger API integration (`POST /triggers` with a link to the passage).

**To evaluate**
- **Generalisation.** 42/42 is measured on the development set, and prompt and predicate rules were
  refined while looking at its errors (all as general rules, never case-specific). The hidden set is
  the real test.
- **Extractor recall.** If the model does not create an event, the result is a silent no alert; the
  rules cannot recover it. Only the per-family schema makes incomplete answers visible.
- **Threshold usefulness.** Whisper's quality score barely separates clean from noisy audio (averages
  0.74–0.81 in both) and is estimated per decoding window, so the threshold has little leverage.
  Calibrate on the hidden set or replace it with a better signal.
- **ASR.** Whisper translates Swiss German into Standard German and misses some keywords (for example
  D02: 7 hits in the script, 6 clean, 3 noisy). Word error rate against the scripts has not been
  measured.
- **Reproducibility.** Claude's temperature cannot be set; repeatability relies on the extraction
  cache keyed by model, prompt version, policies and transcript.
- **Operations.** The Claude CLI is bound to the account's session limits (one evaluation hit them;
  the pipeline correctly fell back to review and counts these as extraction errors).
- **Speakers.** No diarization: customer and adviser are inferred from content only.

**Compliance with the case rules**
The README asks for self-hostable models in the core pipeline. The team chose Claude Sonnet 5 (chat)
and OpenAI `whisper-1` (ASR) for this prototype; the router prints a warning for both. Moving to
self-hosted models (for example vLLM + an open instruct model, and a Whisper server such as speaches)
is a profile change plus `eval --smoke` and a full evaluation; the quality of smaller open models on
this task has not been measured.
