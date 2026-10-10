# CallGuard evaluation report

One report, built only from logged runs (`runs/eval-<timestamp>-<mode>/summary.json` and `manifest.json`),
not from memory or from numbers quoted elsewhere. For design and what remains open see
[`PIPELINE.md`](PIPELINE.md); for the day-by-day log and decisions see [`PLAN.md`](PLAN.md).

## Read this before the numbers

- **42 audio files are 21 scripted dialogues, each in a clean and a noisy take — not 42 independent
  scenarios.** A metric computed over "42 calls" double-counts every dialogue. Where it matters, this
  report also gives the 21-dialogue view.
- **This is the development set.** Prompt and policy rules (`extract-6`, `policies-0.5`) were refined by
  looking at errors on these same 21 dialogues. Every fix was written as a general rule (never a
  reference to a name, amount or wording of one specific case — checked in `PLAN.md`'s step 3 log), but
  the numbers below still measure fit to a set the system was tuned against. They are not a substitute
  for the hidden test set.
- **The ASR-quality threshold is a heuristic, not a probability.** A manual audit (§5) found it does not
  track real transcription correctness on this corpus; its headline effect is reported honestly (§4.4).
- **"Evidence audit" below is a text-level check by the author of this report against the scripts, not an
  independent human listening to the audio.** No audio-playback tool is available to the agent producing
  this report. Treat §5 as a grounded but non-independent spot-check, not a compliance sign-off.

## 1. What produced these numbers

| | Text runs (scripts) | Audio run (42 WAVs) |
|---|---|---|
| Chat model | `claude-sonnet-5` via Claude Code CLI | `claude-sonnet-5` via Claude Code CLI |
| ASR | — (script text used directly) | `whisper-1` (OpenAI) |
| Prompt version | `extract-6` | `extract-6` |
| Policies version | `policies-0.5` | `policies-0.5` |
| Self-hostable (per `manifest.json`) | chat: **no** | chat: **no**, ASR: **no** |
| Run folders | `runs/eval-20261010-142614-speakers/`, `runs/eval-20261010-143021-nospeakers/` | `runs/eval-20261010-124415-audio/` |

**Compliance note.** The README asks for self-hostable models in the submitted pipeline. The team's
explicit, recorded decision (`PLAN.md` §1, §9) is to develop and currently run on Claude Sonnet 5 and
OpenAI `whisper-1`; `manifest.json` now records `self_hostable_compliant: false` for every such run and
`callguard/pipeline.py` prints a warning. Moving to self-hosted endpoints is a profile change in
`config/models.json` (see `PIPELINE.md` §2.5) followed by `eval --smoke` and a full re-evaluation — the
numbers below do **not** transfer to a different model and must not be presented as if they did.

## 2. Headline metrics

### 2.1 Three-class confusion matrix (audio, 42 calls)

| gold \ predicted | alarm | review | no_alert |
|---|---|---|---|
| **alarm** | 16 | 0 | 0 |
| **review** | 0 | 6 | 0 |
| **no_alert** | 0 | 0 | 20 |

Every cell, including the zero ones, comes straight from `summary.json`; none are hidden inside an
aggregate "review" bucket.

### 2.2 Alarm precision / recall and escalation direction (audio)

| Metric | Value | What it shows |
|---|---|---|
| Alarm precision | 16/16 | Every raised alarm matched an expected alarm (no false alarms) |
| Alarm recall | 16/16 | Every expected alarm was raised, not just triaged to review |
| False alarms | 0/26 | Of the 26 non-alarm calls, none became a false alarm |
| alarm → review (missed escalation, still triaged) | 0/16 | — |
| alarm → no_alert (missed without triage) | 0/16 | The strictly dangerous failure mode: zero occurrences |
| review → alarm (unjustified confident escalation) | 0 | — |
| review → no_alert (uncertainty lost) | 0 | — |

### 2.3 Review rate and technical failures (audio)

| Metric | Value |
|---|---|
| Review rate | 6/42 (14%), all 6 are `missing_policy_fact` (C17, C19, C20, clean and noisy) |
| Extraction errors (model unreachable, not a classification) | 0/42 |
| `evidence_hit` | n/a on audio — Whisper segments are not script turns, so the text-overlap metric used on
  scripts does not apply; see §5 for the audio evidence check instead |

### 2.4 Clean vs. noisy and by family (audio)

| Split | Correct |
|---|---|
| Clean (K1/K3) | 21/21 |
| Noisy (K2/K4) | 21/21 |
| Stufe 1 (keyword-explicit) | 12/12 |
| Stufe 2 (context pairs) | 24/24 |
| Stufe 3 (open review cases) | 6/6 |

**By dialogue (21), not by take (42):** 8 of 21 dialogues are scripted as `alarm`, 10 as `no_alert`, 3 as
`review` (16/20/6 when counted per take, exactly double, confirming no dialogue split its two takes across
different labels); both takes of every dialogue landed on the expected label.

### 2.5 Text runs (scripts), current code

| Mode | Accuracy | Notes |
|---|---|---|
| With speaker labels | 21/21 (1.000) | `runs/eval-20261010-142614-speakers/` |
| Without speaker labels | 20/21 (0.952) | One miss: `Stufe2_C06` → `review/technical_uncertainty` (see §3) |

### 2.6 Latency and cost (`extraction_time_s` in each run's `summary.json`)

| Run | Total | Avg per uncached call | Cached calls |
|---|---|---|---|
| Text, with speakers (5-call smoke, 2026-10-10 14:22) | 287.2 s | 57.4 s | 0/5 |
| Audio, 42 calls (`eval-20261010-124415-audio`) | not re-timed this pass (older run, before `elapsed_s` instrumentation) | — | — |

Latency capture (`_meta.elapsed_s`, wall-clock around the model call) was added in this pass; prior runs
predate it, so audio timing will only be available from the next full re-run. OpenAI's reported Whisper
price is used for the ASR cost estimate in `PLAN.md` (~USD 1.15 for a full 42-call pass); the Claude CLI
does not expose a reusable per-call cost field to the OpenAI-style adapter, so chat cost is not metered
here — this is a known gap, not a hidden number.

## 3. Known current miss (text, no speaker labels)

`Stufe2_C06` (trade, gold `alarm`) comes back `review / technical_uncertainty`. Cause, read from the
canonical result: the model's quote for `own_trade_request` does not exactly match its cited segment or
the immediate neighbour (a copy slip under the strict, no-fuzzy-matching grounding the project settled
on — see `PLAN.md` for that decision). This is an **extraction-layer** error, not an ASR error (no audio
involved) and not a policy error (the rule engine correctly downgraded the unverifiable "true" to
`unknown` and escalated to review, which is the safe direction: a missed alarm is strictly worse than an
unnecessary review). It may or may not reproduce on a re-run (Claude's temperature is not configurable).
Not yet fixed; tracked here rather than silently re-run until it disappears.

## 4. Evidence audit (text-level spot-check, not independent)

Extending the check already in `PIPELINE.md` §2.3, every decisive quote on the 42 audio calls was
compared against the original script, separating what kind of error would show up at each layer:

| Layer | What was checked | Finding |
|---|---|---|
| **ASR** (Whisper output vs. script) | Every spoken access/confirmation code (`C09`, `C10`, `C17`, `D06`, clean and noisy) | All digits transcribed correctly in every version. Two reproducible noise-induced word substitutions found and fixed as keyword variants (`PIPELINE.md` §2.3): "Quartalszahlen"→"Quantauszahlen", "vor der Meldung"→"von der Meldung", both only on noisy takes, both in two independent dialogues |
| **Extraction** (LLM quote vs. real transcript) | 93 decisive quotes on `present` events | All exact-matched or close variants of the real wording; none invented. The one extraction-layer failure currently known is §3 above (text, not audio) |
| **Policy** (rule outcome vs. the quoted evidence) | Whether a quoted passage actually establishes the condition it is attached to | No case found where a correctly-anchored quote was attached to the wrong condition; D02's turn T012 ("Denn lömmer de Kauf" = let's abandon the purchase) was previously miscited as a purchase request and is now excluded by an explicit prompt rule (`PLAN.md` step 3) |

This is **not** equivalent to a person listening to the recordings: it is a text comparison against the
scripts, done by the same agent that tuned the prompt, so it cannot claim independence. It is reported as
what it is — a grounded internal check — not as sign-off.

## 5. What this report does not establish

- **Generalisation to the hidden test set.** Not measured. Section "Read this before the numbers" applies.
- **An independent review.** A genuinely independent check needs a second person to write new cases
  blind to the current prompt and to listen to the audio; this report's author cannot do either (see
  `PIPELINE.md` for the explicit request to a human for a blind challenge set, not fabricated here).
- **Self-hosted-model performance.** Every number above is Claude Sonnet 5 + `whisper-1`. Untested on any
  self-hostable model.
- **Threshold calibration.** The three presets (sensitive 0.5 / balanced 0.6 / conservative 0.75) move at
  most one of the 42 audio calls between labels (`PIPELINE.md` §2.3); this is reported as a limitation,
  not hidden.
