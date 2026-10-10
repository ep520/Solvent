# CallGuard pipeline

How a Swiss German bank call becomes an **alarm**, **review** or **no alert**, what is implemented and
verified, and what is still open. Diagrams for every flow in this document are in
[`PIPELINE_FLOW.md`](PIPELINE_FLOW.md); the single evaluation report (metrics, caveats, audit) is
[`EVALUATION.md`](EVALUATION.md); the day-by-day work log and decisions are in [`PLAN.md`](PLAN.md); the
case brief is [`README.md`](README.md).

## 1. Overview

```
data/Audio/*.wav (mono, 16 kHz, Swiss German)
   │
   ▼  callguard/asr.py ─ Whisper (whisper-1) via the model router, cached by file content hash
segments [{id, start, end, text, avg_logprob}]          → data/Transcriptions/*.json|.md
   │
   ├─ extract.keyword_hits ─ Stichwortliste.json phrases → cached-transcript highlights and coverage only
   │
   ▼  callguard/extract.py ─ ONE structured LLM request per call (Claude Sonnet 5), cached
facts: {"families": {<enabled family>: {"events": [{conditions: {<predicate>: {state, evidence}}}]}}}
   │      state ∈ true | false | unknown, evidence = segment id + short quote
   │      a family a bank switched off is left out entirely, not just empty
   ▼  callguard/decide.py ─ pure, deterministic code
   1. anchor every quote in the transcript with strict text matching
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
- Whisper has no diarization: segment speakers remain unknown. The extractor may mark an event actor as
  `customer` or `advisor` only as `inferred`, with supporting segment IDs; otherwise the role is `unknown`.
- No audio preprocessing: the files are already mono 16 kHz and under the 25 MB API limit.

### 2.2 Fact extraction (`callguard/extract.py`, prompt version `extract-6`)

- One request per call with the **whole** transcript, every time. There is no keyword pre-filter,
  because Level 2 and 3 calls contain no listed keyword at all.
- The JSON schema requires every family; `"events": []` means "assessed, nothing found", so a missing
  family exposes an incomplete answer. Every event also carries an actor role: `customer` or `advisor` is
  `inferred` only with supporting segment ids; otherwise it is explicitly `unknown`.
- Prompt rules that matter: `true` only when a passage establishes the condition, `false` only when a
  passage explicitly excludes it, otherwise `unknown` (including "I don't know" and "not proven"); an
  ambiguous object makes dependent conditions `unknown`; a later refusal neither undoes a request nor
  proves one; quotes are 3–10 consecutive words copied from one segment.
- Families and predicates live in [`config/policies.json`](config/policies.json) (`policies-0.5`):

| Family | Alarm requires all of |
|---|---|
| trade | `own_trade_request`, `information_nonpublic`, `information_market_relevant`, `request_based_on_information` |
| access | `value_spoken`, `access_secret`, `secret_active` |
| disclosure | `advisor_disclosed`, `third_party_detail`, `authority_absent` |
| documentation | `concealment_requested`, `fact_relevant`, `designated_recipient` |
| splitting | `split_requested`, `movements_connected`, `evasion_purpose` |
| numbers | none: classifies identifiers read out (card number, masked card digits, invoice or reservation reference, account/IBAN), **never an alarm**; money amounts, dates and quantities produce no event (less output for local models) |

- **Bank-configurable families.** Each family has an `enabled` flag in `policies.json` (default `true`).
  Disabling one removes it from both the prompt and the JSON schema — smaller request, not a "missing
  family" technical gap — and `decide()` treats it as absent by configuration. Disabling or re-enabling a
  family changes the extraction cache key, so it correctly forces re-extraction, never a stale reuse.

### 2.3 Decision (`callguard/decide.py`)

Two deterministic techniques only.

**1. Evidence anchoring.** Each quote must occur in its exact cited segment after whitespace
normalisation and case-folding; punctuation is retained. A cross-segment quote may start in the cited
segment and continue only into its immediate successor. Segment IDs must exist exactly: there are no ID
aliases, backward-neighbour fallbacks or approximate matches. Timestamps, segment IDs and playable
±10-second clip bounds are derived only from the grounded ASR segments. A `true` or `false` without
anchored evidence becomes `unknown` and carries a specific grounding issue; unrelated events can still
resolve the call to `alarm` or `no_alert`.

**2. Three-valued rules.** Per event: any `false` → absent (no alert); else any `unknown` → undetermined
(review); else present. A present event becomes an **alarm** only if every decisive condition has at
least one quote whose ASR quality `exp(avg_logprob)` reaches the threshold.

**Exact aggregation** (`decide.evaluate_event`): for each decisive (`true`) condition, take the **best**
(maximum) quality among its own quotes — one strong quote is enough even if the model also cited a weaker
one (`test_best_of_several_quotes_decides_a_condition_quality`). A condition whose quotes carry no score
at all (a script, or an ASR response without logprobs) is listed in `quality_unavailable` and is **never**
treated as 0 or as 1.0, so it cannot trigger a downgrade on its own
(`test_missing_quality_is_reported_not_replaced_by_a_perfect_score`). If any decisive condition's best
score is below the threshold, the call goes to `review / below_escalation_threshold`, never straight to
`no_alert` (`test_low_asr_quality_on_a_decisive_condition_is_review_below_threshold`); the threshold only
ever acts on otherwise fully-supported events, never on `unknown` or excluded ones
(`test_threshold_never_changes_unknown_or_excluded_events`).

Only predicates configured with a role requirement use actor inference (`trade.own_trade_request` requires
`customer`; `disclosure.advisor_disclosed` requires `advisor`). An unresolved role changes just that necessary
predicate to `unknown`, with reason `actor_role_unknown`; a grounded explicit `false` remains an exclusion.

Review always shows its reason:

| Reason | Meaning |
|---|---|
| `missing_policy_fact` | The call does not establish a required fact; the open question is shown |
| `actor_role_unknown` | A condition needs to know who is speaking (customer or adviser) and that role was not inferred |
| `technical_uncertainty` | Extraction failed entirely, a family is missing from the response, or evidence could not be anchored |
| `below_escalation_threshold` | All conditions supported, but a decisive passage was transcribed with low quality |

**Threshold.** `escalation.presets` = sensitive 0.5, balanced 0.6 (default), conservative 0.75. It is an
ASR-quality heuristic, not a fraud probability. When no score exists (scripts), quality is reported as
unavailable and nothing is downgraded.

**Manual spot-check on real audio (2026-10-10), clean and noisy pairs.** Of every decisive quote cited
across the 42 audio calls (93 quotes on `present` events), none scored below 0.6; 10 scored between 0.66
and 0.75 (the conservative preset's cutoff). Reading those 10 against the scripts: **all are faithful
transcriptions** — no flipped negation, no wrong digit, no changed meaning (for example `Stufe1_D02-K2`
"nicht aus einer öffentlichen Meldung" and "möchte ich diese Aktie jetzt kaufen" score 0.72–0.74 while
exactly matching the script's "nöd us ere öffentliche Meldig" / "möcht ich die Aktie jetzt chaufe"). A
separate check of every spoken access code or confirmation number on both clean and noisy takes
(`Stufe2_C09`, `Stufe2_C10`, `Stufe3_C17`, `Stufe1_D06`) found **every digit transcribed correctly** in
every version, with quality scores (0.71–0.83) that do not noticeably differ between clean and noisy
audio. **Conclusion: the threshold mechanism itself is correct (never auto-downgrades, never fabricates a
score), but on this dataset it is not calibrated** — Whisper's per-window quality estimate does not track
actual transcription correctness or noise level here. Treat it as a coarse flag for a person to glance at,
not as a measure that predicts error. A corpus with genuine mistranscriptions (this synthetic TTS set is
unusually clean even when "noisy") would be needed to calibrate or replace it.

### 2.4 Outputs

- `python3 -m callguard.pipeline eval [--audio]` writes one canonical result per call — label, `status`
  (`ok` or `failed`, so a model/network failure is never confused with a genuine review), reasons,
  events, conditions, quotes with times and quality, keyword hits, disabled families, model, prompt and
  policies versions — to `runs/eval-<timestamp>-<mode>/<call>.json`, plus `summary.json` with the metrics
  and `manifest.json` with what produced the run (`EVALUATION.md` §1).
- `callguard/evidence.py` cuts ±10 s clips from the **original** WAV (standard library `wave`).
- `callguard/ui.py` serves the dashboard over cached results (see §2.6). Its local `POST /api/keywords`
  endpoint validates and atomically saves the keyword configuration; it never calls ASR or an LLM.

### 2.5 Model router (`callguard/models.py`, `config/models.json`)

Standard library only, two frozen adapters: `claude_cli` (`claude -p --model claude-sonnet-5` with a
JSON schema, no tools, no user settings) and `openai_compatible` (OpenAI, vLLM, Ollama, llama.cpp,
speaches, private gateways). Switching model is a profile change (`CALLGUARD_CHAT`, `CALLGUARD_ASR` or
`config/models.json`); no code changes. Any switch must pass `eval --smoke` before a full evaluation.

**Local open models (target deployment).** The pipeline is meant to run on local open models; the
router is tuned for efficiency there:
- `json_mode: json_schema` (Ollama, vLLM, llama.cpp server): constrained decoding, so the JSON schema
  costs no prompt tokens. Servers without it use `json_object`/`none`, which put a compact skeleton
  of the expected shape in the prompt (about 2,600 characters instead of the 9,000-character schema).
- `context_tokens` per profile (16,384 for `ollama` and `private`): a call that does not fit (estimated
  at ~3 characters per token plus the output budget) is refused and becomes `technical_uncertainty`,
  instead of being silently truncated by the server. The longest development call (C05/C06, ~9.5 min)
  needs about 4,700 prompt tokens (about 8,800 with the output budget) with the schema out of the prompt.
- `max_output_tokens` (4,096) caps generation time.
- The server must be started with a matching context: Ollama with `OLLAMA_CONTEXT_LENGTH=16384` (its
  OpenAI-compatible endpoint ignores per-request context settings), vLLM with `--max-model-len`,
  llama.cpp with `-c`.
- Local ASR: the `speaches` profile (faster-whisper behind an OpenAI-compatible endpoint) replaces
  `whisper-1` without code changes.

### 2.6 Dashboard (`callguard/ui.py`)

Lists every analysed call with assessment, policy conditions, supporting passages, the original audio
(jump buttons start 10 s before each passage), downloadable clips, the threshold presets and keyword
highlights. Three states are shown apart, not folded into Alarm/Review/No alert:

| State | When |
|---|---|
| **Pending** | No cached extraction yet for this WAV |
| **Failed** | A cached result exists but `status: "failed"` (technical failure) — needs a retry, not a compliance read |
| **Alarm / Review / No alert** | A completed, deterministic decision |

Two `de` keyword variants were added from real Whisper output, not invented: noisy audio consistently
mishears "Quartalszahlen" as "Quantauszahlen" and "vor der Meldung" as "von der Meldung", reproduced in
two independent dialogues (`Stufe1_D02`, `Stufe1_D03`) — see `AsrKeywordVariantTest` in `test_decide.py`
for the evidence and why no further terms were added from a systematic scan of the 42 transcripts.

Switching the threshold preset only reruns `decide()` on the already-cached facts — no ASR or LLM call
(`test_build_data_never_calls_the_chat_model`); a stricter preset can only move a call towards
`review`/`no_alert`, never towards `alarm` (`test_every_preset_is_a_pure_recompute_of_the_same_cached_extraction`).
The keyword panel supports add/edit/remove/enable/disable with validation (empty or duplicate terms
rejected) and atomic save; saving only reruns matching, highlights and counts over cached transcripts —
never a classification (`test_suspicious_event_without_a_keyword_remains_an_alarm`,
`test_innocent_keyword_occurrence_does_not_change_a_no_alert`). The dashboard never calls a model on its
own.

## 3. Results

Full numbers, confusion matrix, latency, caveats and the evidence audit are in
[`EVALUATION.md`](EVALUATION.md); the day-by-day history is in `PLAN.md` §8. Headline, current code
(`extract-6`/`policies-0.5`), Claude Sonnet 5 + `whisper-1`:

| Input | Dialogues | Correct | False alarms | Alarms found |
|---|---|---|---|---|
| Audio, clean (K1/K3) | 21 | **21/21** | 0 | 8/8 |
| Audio, noisy (K2/K4) | 21 | **21/21** | 0 | 8/8 |
| Scripts, with speaker labels | 21 | 21/21 | 0 | 8/8 |
| Scripts, without speaker labels | 21 | 20/21 | 0 | 7/8 |

**Known current miss:** `Stufe2_C06` (text, no speaker labels) comes back `review/technical_uncertainty`:
the model's quote for `own_trade_request` fails strict grounding (a copy slip, not an ASR or policy
error). Correctly routed to review rather than a missed alarm, but not yet fixed (`EVALUATION.md` §3).

Threshold sensitivity (42 audio calls): sensitive and balanced give identical results; conservative moves
one call (`Stufe1_D02-K2`, noisy, quality 0.72) from alarm to review — the ASR-quality heuristic has
little leverage on this corpus (§2.3).

## 4. How to run

```bash
cp .env.example .env                                   # then set OPENAI_API_KEY (only needed for new audio)
python3 -m unittest discover -s tests                  # 106 tests, no external model calls or keys
python3 -m callguard.models                            # active chat / ASR profiles
python3 -m callguard.asr data/Audio --workers 4        # transcribe (cached; skips known files)
python3 -m callguard.pipeline audio data/Audio/Stufe1_D02-K1.wav   # one call, text explanation
python3 -m callguard.pipeline eval --audio --workers 6 # all 42 WAVs with metrics
CALLGUARD_CHAT=private CALLGUARD_ASR=private python3 -m callguard.pipeline eval --audio --require-self-hostable --workers 6
python3 -m callguard.pipeline eval --smoke --workers 5 # 5-call regression set (scripts)
python3 -m callguard.ui                                # dashboard on http://127.0.0.1:8090
```


## 5. Tests (106)

| Suite | What it guards |
|---|---|
| `test_decide.py` | rules, strict anchoring, grounding failures, threshold and quality aggregation, all four review reasons, role uncertainty, bank-configurable families (`enabled` flag excluded from prompt/schema, not a technical gap), `status: ok/failed` distinct from the label, and challenge regressions: active/expired/unknown access codes, a trade request preserved after an adviser refusal, real ASR keyword variants, and a supported event with no keyword hit; keyword matcher reproduces every annotated hit |
| `test_policies.py` | hand-written **oracle** extractions for all 21 scripts must give the expected assessment and cite an expected turn; **replay** of 42 real Sonnet extractions (`extract-1`) through the current rules; config/fixture consistency |
| `test_models.py` | router with a fake HTTP server and a fake `claude` command, `.env` loading, retries, compact-schema skeleton and context-window guard for local models |
| `test_asr.py` | segment annotation, exports, timestamp validation |
| `test_ui.py` | clips, mapping to dashboard fields per threshold, server routes, Range requests, path safety, keyword coverage/validation/atomic save, proof that a threshold switch never calls a model and never raises a classification, and that keyword changes never change one |

Mutation checks confirmed the policy tests fail when a condition is removed, when "undetermined" is
treated as no alert, or when the threshold is broken.

## 6. What is implemented and verified

- [x] Transcription of all 42 calls, hash-cached, exported and checked (complete coverage, matching
      hashes, no prompt leakage).
- [x] Five fraud checks plus number classification, as configuration, with one extraction per call.
- [x] Deterministic decisions with explicit reasons and open questions; uncertainty goes to review.
- [x] Evidence anchored to the real transcript, with timestamps, quality and ±10 s audio clips.
- [x] Adjustable threshold (three presets, editable without code) with a visible effect.
- [x] Keyword coverage and local keyword management: families, enabled terms, DE/Swiss-German variants,
      observed occurrences, and validated persistence. Matching remains independent from decisions.
- [x] Metrics on false alarms and missed cases, split by level and by clean/noisy audio.
- [x] Review dashboard connected to the pipeline, tested in a browser; Pending and Failed calls shown
      apart from Alarm/Review/No alert (§2.6).
- [x] No manual step per call: audio in, canonical result and clips out.
- [x] Bank-configurable checks: each family can be switched off in `policies.json` without code (§2.2).
- [x] Every `eval` run freezes what produced it into `manifest.json` — model, profile, self-hostability,
      prompt/policies versions and hashes, thresholds — plus per-call `elapsed_s` timing.

## 7. What is missing or still to evaluate

**To do**
- [ ] Fix the known current miss, `Stufe2_C06` text/no-speakers (§3).
- [ ] **Independent audit, by someone other than the author of the prompt and policies:** a blind
      10–15-case challenge set (paraphrases, ambiguous roles, missing information, innocent-but-keyword
      calls, suspicious-but-keyword-free calls) and a real listen to a sample of clean/noisy clips. The
      text-level evidence check already done (`EVALUATION.md` §4) is grounded but not independent — it
      was written by the same agent that tuned the prompt, and no audio-playback tool is available to it.
- [ ] Release run: a final `eval` on scripts and audio with the frozen configuration, and a `freeze` of
      the extractions into `tests/fixtures/replay/claude-sonnet-5/extract-6/`.
- [ ] Demo preparation: one call from audio to alarm with clips, a threshold or keyword change with a
      visible effect, a borderline call (D03: keywords present, no alert), the metrics.
- [ ] Hidden test set (30 % of the material, run on Sunday): transcribe, `eval --audio`, report — with the
      configuration and threshold frozen **before** seeing the labels, never calibrated against them.
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
- **ASR.** Whisper translates Swiss German into Standard German and misses dialect-only (`gsw`) keyword
  variants by design (`de` variants cover the family instead); a noise-induced mishearing pattern was found
  and closed for two families (§2.6), but the underlying word error rate against the scripts has not been
  measured, and other families may have similar uncaught noise artifacts.
- **Reproducibility.** Claude's temperature cannot be set; repeatability relies on the extraction
  cache keyed by model, prompt version, policies and transcript.
- **Operations.** The Claude CLI is bound to the account's session limits (one evaluation hit them;
  the pipeline correctly fell back to review and counts these as extraction errors).
- **Speakers.** No diarization: customer and adviser are inferred from content only.

**Compliance with the case rules**
The README asks for self-hostable models in the core pipeline. The team's recorded decision (`PLAN.md`)
is to develop and currently run on Claude Sonnet 5 (chat) and OpenAI `whisper-1` (ASR), both not
self-hostable; every `eval` run now records this automatically in `manifest.json`
(`self_hostable_compliant: false`) and the router prints a warning. `--require-self-hostable` fails fast
instead of silently running a non-compliant profile. Moving to self-hosted models (for example vLLM + an
open instruct model, and a Whisper server such as `speaches`) is a profile change in `config/models.json`
plus `eval --smoke` and a full evaluation (§2.5); the quality of smaller open models on this task has not
been measured, and the numbers in §3/`EVALUATION.md` do not transfer to a different model.
