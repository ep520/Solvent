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
   ▼  callguard/asr.py ─ Whisper via the model router, cached by file content hash
segments [{id, start, end, text, avg_logprob}]          → cache/asr/<hash>.json
   │                         └─ asr.export (ASR CLI or ingest) → data/Transcriptions/*.json|.md
   │
   ├─ keyword matchers ─ Stichwortliste.json → cached-transcript highlights and coverage
   │                    └─ also feeds snippet selection (with cue words and spoken digits)
   │
   ├─ callguard/snippets.py ─ selected passages for dashboard highlights and audio navigation only
   │                          (config/snippets.json; max. 12 snippets)
   ▼  callguard/extract.py ─ at most ONE structured LLM request per call over the complete transcript
facts: {"families": {<enabled family>: {"events": [{conditions: {<predicate>: {state, evidence}}}]}}}
   │      state ∈ true | false | unknown, evidence = segment id + short quote
   │      a family a bank switched off is left out entirely, not just empty
   ▼  callguard/decide.py ─ pure, deterministic code
   1. anchor every quote in the transcript with strict text matching
   2. three-valued rules per family
   3. ASR-quality escalation threshold
   4. call label = max(alarm > review > no_alert), reason codes, template explanation
   5. post-decision rationale = short deterministic template pointing to the deciding conditions and grounded evidence
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

### 2.2 Fact extraction (`callguard/extract.py`, prompt version `extract-7`)

- At most one request per call. For the MVP, the model always receives the **complete transcript**, within
  the context budget. It assesses every enabled family from that baseline; no keyword, cue word or digit
  match can suppress detection.
- `callguard/snippets.py` remains active only for dashboard highlighting and jump-to-audio passages.
  Snippet hits are neither evidence nor routing. Routing/adaptive prompting is deferred until recall and
  latency have been measured against this full-transcript baseline.
- The extraction cache key includes the chat profile/model, prompt and schema, policies, full rendered
  transcript and human-calibration examples. Keyword or snippet configuration changes only alter review
  highlights; they do not require a fresh extraction.
- The JSON schema requires every family; `"events": []` means "assessed, nothing found", so a missing
  family exposes an incomplete answer. Every event also carries an actor role: `customer` or `advisor` is
  `inferred` only with supporting segment ids; otherwise it is explicitly `unknown`.
- Prompt rules that matter: `true` only when a passage establishes the condition, `false` only when a
  passage explicitly excludes it, otherwise `unknown` (including "I don't know" and "not proven"); an
  ambiguous object makes dependent conditions `unknown`; a later refusal neither undoes a request nor
  proves one; quotes are 3–10 consecutive words copied from the cited segment, or from that segment
  into its immediate successor when needed.
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
  events, conditions, quotes with segment IDs/times/quality and clamped clip bounds, keyword hits and
  disabled families — to `runs/eval-<timestamp>-<mode>/<call>.json`. New release artefacts also add a
  backward-compatible traceability envelope: `schema_version`, `run_id`, `policy_version`, recording
  ID/hash, deterministic readable explanation, a short post-decision `rationale`, evaluated policy-rule
  outcomes, actual alarm-generating rules, and separate missing-fact / extraction / grounding / provider
  uncertainty. `rationale` never asks a model for prose or adds facts: alarm points to supported
  conditions; no-alert points to an excluded condition or no candidate event; review points to the
  missing fact or technical blocker. See
  `CANONICAL_RESULT_SCHEMA.md`; legacy cached results without the envelope remain readable.
  The run folder also contains:
  `summary.json` (three-class metrics, unresolved and technical counts, paired clean/noisy analysis and
  threshold comparison), `manifest.json` (release identity/configuration/cache contract/audio hashes),
  `recordings.csv`, `errors.json`, and an intentionally unfilled `human-audit-checklist.md`.
  The folder and its initial manifest are created before the batch starts. Every completed call atomically
  replaces its per-call JSON and then updates the manifest and partial summary; an interrupted batch is
  explicitly marked `in_progress` or `interrupted` rather than being mistaken for a final evaluation.
  A fresh invocation reuses the already-complete ASR and extraction cache entries.
  Cached values are not presented as uncached latency.
- `callguard/evidence.py` cuts ±10 s clips from the **original** WAV (standard library `wave`).
- `callguard/ui.py` serves the dashboard over cached results (see §2.6). Its local `POST /api/keywords`
  endpoint validates and atomically saves the keyword configuration; it never calls ASR or an LLM.
  `POST /api/policies` supports local add/edit/delete of policy families and conditions, with schema
  validation and atomic writes. A policy edit deliberately invalidates old extraction caches and requires
  a fresh evaluation; it is never applied retrospectively to old results.
- A reviewer can resolve an automatic `Review` as **Alert** or **No alert** in the dashboard. The outcome
  is atomically retained under `data/ReviewFeedback/` and becomes a small, bounded calibration example
  for later extractions; it is included in the extraction cache key and never overwrites an existing
  automatic result or policy rule. The reviewed call itself is excluded from its own calibration prompt.
- Every exported ASR segment can be corrected in the dashboard. Corrections are stored as overlays under
  `data/Corrections/`, retain the original ASR wording, and are ignored if a newer ASR run changes that
  original segment. The next pipeline run reads the corrected wording and creates a new extraction cache
  entry; until then the dashboard labels the prior assessment as requiring re-evaluation.

Direct `pipeline eval --audio` uses the ASR cache but does not create the tracked
`data/Transcriptions` exports. Use `python3 -m callguard.asr` or `callguard.ingest` when the dashboard
needs those exports.

**Keyword provenance.** `data/Stichwortliste.json` has 11 configurable keyword IDs, 58 DE/Swiss-German
spellings and 47 distinct normalised terms. The file is based on the public Outcept list (the former
`Outcept/trigger-api` URL redirects to `Outcept/inventx-case-study`); comparison on 2026-10-10 found its
45 upstream distinct terms plus two local, documented additions: `K03.de: Quantauszahlen` and
`K04.de: von der Meldung`. Keyword matches never directly decide a classification. In the MVP they
influence dashboard highlights and audio navigation only, so changing them never changes the
full-transcript extraction input.

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

**Self-hosted profiles added (2026-10-10, integrated from `ep520/Solvent#2`).** `config/models.json`
now has `qwen3` (Qwen3 Thinking on vLLM, reasoning on), `qwen3_fast` (hybrid Qwen3 with thinking off,
for 24 GB GPUs), `ollama_qwen3` (laptop development only) and `whisper_local` (any self-hosted
OpenAI-compatible Whisper endpoint). Supporting changes in `models.py`: a `<think>...</think>` block is
stripped from the reply before JSON parsing (reasoning models without a server-side reasoning parser put
it inline, and it can contain braces that would otherwise confuse the JSON search), `extra_body` passes
server-specific decoding options (`top_p`, `top_k`, `chat_template_kwargs`), the served model name can
come from an environment variable (`chat_model_env`/`asr_model_env`, set by `runpod/env.sh`), and
temperature is per-profile (Qwen3 Thinking needs 0.6, not 0, or greedy decoding can loop). `runpod/`
holds the deployment scripts (`setup.sh` starts vLLM sized to the GPU, `run_eval.sh` runs an evaluation
against it); see `runpod/README.md`.

### 2.6 Dashboard (`callguard/ui.py`)

Lists every analysed call with assessment, policy conditions, supporting passages, the original audio
(jump buttons start 10 s before each passage), downloadable clips, threshold presets, keyword highlights
and the exact model input. Input/cache state is shown separately from Alarm/Review/No alert:

| State | When |
|---|---|
| **Pending** | The transcript export or a compatible extraction cache is missing for this WAV |
| **Current** | The cached extraction was made with the current keyword/snippet input |
| **Stale input** | A cached extraction exists, but its earlier keyword/snippet input differs from the current configuration; rerun the pipeline |
| **Alarm / Review / No alert** | A completed, deterministic decision |

Two `de` keyword variants were added from real Whisper output, not invented: noisy audio consistently
mishears "Quartalszahlen" as "Quantauszahlen" and "vor der Meldung" as "von der Meldung", reproduced in
two independent dialogues (`Stufe1_D02`, `Stufe1_D03`) — see `AsrKeywordVariantTest` in `test_decide.py`
for the evidence and why no further terms were added from a systematic scan of the 42 transcripts.

Switching the threshold preset only reruns `decide()` on the already-cached facts — no ASR or LLM call
(`test_build_data_never_calls_the_chat_model`); a stricter preset can only move a call towards
`review`/`no_alert`, never towards `alarm` (`test_every_preset_is_a_pure_recompute_of_the_same_cached_extraction`).
The keyword panel supports add/edit/remove/enable/disable with validation (empty or duplicate terms
rejected) and atomic save; saving reruns matching, highlights and counts over cached transcripts and
updates the current snippet view, but does not call a model or change the decision from an existing cache
entry. The dashboard never calls a model on its own.

### 2.7 Explainability: counterfactuals (`callguard/counterfactual.py`)

For the main event of every call, `decide()` now also answers "what would have to be different for the
label to change?". Pure functions over the event `decide.evaluate_event` already produced — no model
call, no second read of the transcript — so an explanation can never disagree with the actual decision:
each counterfactual re-applies the exact same three-valued rule to a hypothetical condition state or a
different threshold preset (`test_rule_matches_decide` asserts this equivalence directly). Two kinds:

- **Condition flips:** "If `information_nonpublic` were supported instead of not established, the
  decision would be ALARM" — only flips that actually change the label are kept, ordered so the open
  (`unknown`) facts that would settle a review come first.
- **Threshold flips:** for a present event, which presets would move it between `alarm` and `review`.

`numbers` events (classification only) produce no counterfactuals. The dashboard shows them under *What
would change the decision*; `pipeline text`'s `explain()` prints them as `WHAT-IF` lines.

### 2.8 Automatic ingest (`callguard/ingest.py`)

Closes the README's "no manual step per call" requirement end to end, not just "no manual step inside
`eval`": `python3 -m callguard.ingest` watches `data/Inbox/` (where a bank's call recorder would write).
Each stable new WAV is moved into `data/Audio/`, transcribed, extracted and decided with the normal code
path (`pipeline.run_audio`), written to `runs/ingest/<call>.json`, and — if `--trigger-url` is set —
reported to the Trigger API for `alarm`/`review` calls, with a link straight to the passage in the
dashboard. A file that fails any step is moved to `data/Inbox/failed/` with its traceback next to it, so
one bad recording cannot silently block the folder or disappear. `--once` processes what is there and
exits (for `eval`-style batch use); without it, it polls every `--interval` seconds (default 5 s).

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
python3 -m unittest discover -s tests                  # full suite; no external model calls or keys
python3 -m callguard.models                            # active chat / ASR profiles
python3 -m callguard.asr data/Audio --workers 4        # transcribe (cached; skips known files)
python3 -m callguard.pipeline audio data/Audio/Stufe1_D02-K1.wav   # one call, text explanation
python3 -m callguard.pipeline eval --audio --workers 6 # all 42 WAVs with metrics
CALLGUARD_CHAT=private CALLGUARD_ASR=private python3 -m callguard.pipeline eval --audio --require-self-hostable --workers 6
python3 -m callguard.pipeline eval --smoke --workers 5 # 5-call regression set (scripts)
python3 -m callguard.ui                                # dashboard on http://127.0.0.1:8090
```


## 5. Tests

| Suite | What it guards |
|---|---|
| `test_decide.py` | rules, strict anchoring, grounding failures, threshold and quality aggregation, all four review reasons, role uncertainty, bank-configurable families (`enabled` flag excluded from prompt/schema, not a technical gap), `status: ok/failed` distinct from the label, and challenge regressions: active/expired/unknown access codes, a trade request preserved after an adviser refusal, real ASR keyword variants, and a supported event with no keyword hit; keyword matcher reproduces every annotated hit |
| `test_policies.py` | hand-written **oracle** extractions for all 21 scripts must give the expected assessment and cite an expected turn; **replay** of 42 real Sonnet extractions (`extract-1`) through the current rules; config/fixture consistency |
| `test_models.py` | router with a fake HTTP server and a fake `claude` command, `.env` loading, retries, compact-schema skeleton and context-window guard for local models |
| `test_asr.py` | segment annotation, exports, timestamp validation |
| `test_ui.py` | clips, mapping to dashboard fields (including deterministic supported/excluded fact reasons), server routes, Range requests, path safety, keyword and policy CRUD validation/atomic save, proof that a threshold switch never calls a model and never raises a classification, and that keyword changes do not change an existing cached classification |
| `test_evaluation.py` | three-class metrics, unresolved/failure accounting, dataset contract, manifest identity, and the release-result envelope / legacy-reader compatibility |
| `test_counterfactual.py` | counterfactual rule equivalence with `decide.py`, which fact a review names as decisive, threshold counterfactuals; `callguard.ingest` (processed/moved, quarantine on failure, trigger payload); the Qwen3/local-model router additions (`<think>` stripping, `extra_body`, env-resolved model name, self-hostable profiles) |

Mutation checks confirmed the policy tests fail when a condition is removed, when "undetermined" is
treated as no alert, or when the threshold is broken.

## 6. What is implemented and verified

- [x] Transcription of all 42 calls, hash-cached, exported and checked (complete coverage, matching
      hashes, no prompt leakage).
- [x] Five fraud checks plus number classification, as configuration, with at most one extraction request per call (or the configured no-request fallback).
- [x] Deterministic decisions with explicit reasons and open questions; uncertainty goes to review.
- [x] Evidence anchored to the real transcript, with timestamps, quality and ±10 s audio clips.
- [x] Adjustable threshold (three presets, editable without code) with a visible effect.
- [x] Keyword coverage and local keyword management: families, enabled terms, DE/Swiss-German variants,
      observed occurrences, and validated persistence. Matching never enters the deterministic rules
      directly; it is used only for review highlights and audio navigation.
- [x] Metrics on false alarms and missed cases, split by level and by clean/noisy audio.
- [x] Review dashboard connected to the pipeline, tested in a browser; Pending, Current and Stale-input
      states are shown apart from Alarm/Review/No alert (§2.6).
- [x] No manual step per call: audio in, canonical result and clips out — including true end-to-end
      automation via `callguard.ingest` watching a folder (§2.8), not just batch `eval --audio`.
- [x] Counterfactual explanations ("what would change this decision") for the main event of every call,
      provably consistent with the decision rule (§2.7).
- [x] Self-hosted chat profiles ready to use: Qwen3 Thinking and a fast hybrid variant on vLLM, plus
      Ollama for laptop development, with RunPod deployment scripts (§2.5).
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
- [ ] Run `eval --smoke` then a full `eval --audio --require-self-hostable` against a real `qwen3`/
      `qwen3_fast` deployment (`runpod/setup.sh`): the profiles and the router support exist (§2.5), but
      nobody has run them against the dataset yet. Until then the self-hosted path is untested, not just
      unmeasured.
- [ ] Independently check `tools/snippets/`'s own claim (95% evidence recall with general cues vs 33%
      random): that standalone helper remains analysis-only; the production snippet engine is
      `callguard/snippets.py`, and the recall number itself has not been re-derived outside the tool.

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
  cache keyed by model, prompt/schema version, policies and the exact rendered model input (including
  snippet configuration and keyword list).
- **Operations.** The Claude CLI is bound to the account's session limits (one evaluation hit them;
  the pipeline correctly fell back to review and counts these as extraction errors).
- **Speakers.** No diarization: customer and adviser are inferred from content only.

**Compliance with the case rules**
The README asks for self-hostable models in the core pipeline. The team's recorded decision (`PLAN.md`)
is to develop and currently run on Claude Sonnet 5 (chat) and OpenAI `whisper-1` (ASR), both not
self-hostable; every `eval` run now records this automatically in `manifest.json`
(`self_hostable_compliant: false`) and the router prints a warning. `--require-self-hostable` fails fast
instead of silently running a non-compliant profile. The self-hosted path now has ready profiles
(`qwen3`, `qwen3_fast`, `ollama_qwen3`, `whisper_local`, §2.5) and `runpod/` deployment scripts, so
switching is a profile change plus `eval --smoke` and a full evaluation — but, as above, this has not
been run yet; the quality of these open models on this task is still unmeasured, and the numbers in
§3/`EVALUATION.md` do not transfer to a different model until it has been.
