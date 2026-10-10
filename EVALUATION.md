# CallGuard MVP — release evaluation

This is a release artefact, not a pitch deck. Every current-candidate number is derived from the canonical per-call JSON and `summary.json` in the cited run. It does not claim development-set generalisation to a hidden set.

## Status and evidence hierarchy

| Category | Artefact | Status |
| --- | --- | --- |
| Current candidate: audio | `runs/eval-20261010-164409-audio/` | Verified by this release evaluation; cached ASR/extractions and fresh deterministic decisions plus post-decision rationale |
| Current candidate: scripts with speakers | `runs/eval-20261010-160117-speakers/` | Verified; cached extraction replay |
| Current candidate: scripts without speakers | `runs/eval-20261010-160120-nospeakers/` | Verified; cached extraction replay |
| Historical runs | older `runs/eval-*` folders | Historical comparison only; a run without `manifest.json` is documented, not independently verified |
| Hidden/challenge set | — | Not supplied / not run; no hidden-set claim |

The candidate manifest records Git revision and dirty state, source/config hashes, models, prompt/policy/keyword hashes, cache-key contract, audio hashes and threshold. The working tree was dirty (`git_commit: 709a017`, `dirty: true`), so source hashes—not the commit alone—identify it.

All release runs reused matching caches. Labels are valid for manifested cached inputs, but observed milliseconds are cache-read/decision timings, not an uncached end-to-end latency benchmark.

## Candidate configuration

| Field | Audio candidate |
| --- | --- |
| Chat | `claude_code` / `claude-sonnet-5`; `extract-6`; temperature 0; seed 7; retry 1 |
| ASR | `openai` / `whisper-1` |
| Policy / grounding | `policies-0.5`; `exact_whitespace_casefold` |
| Keyword config | SHA-256 `11b4f1…15e3e` |
| Primary threshold | `balanced`, ASR-quality 0.60 |
| Workers | 6 |
| Self-hostable compliant | **No**: both manifest components declare `self_hostable: false` |
| Model revision | Not exposed by providers; model identifiers only |

The threshold is an ASR-quality escalation heuristic, **not** a fraud probability. A model/profile, prompt, policy, schema, keyword, audio-hash or threshold change requires smoke plus full re-evaluation; these results do not transfer to another model.

## Dataset and gold contract

The audio contract in `runs/eval-20261010-164409-audio/summary.json` validates **42 recordings**, **21 unique dialogues**, one clean and one noisy take per dialogue, and no missing, duplicate or invalid gold label. Every audio row records:

```text
recording_id, dialogue_id, audio_path, audio_hash, variant,
split, gold_label, gold_source, family, gold_event, gold_evidence
```

- K1/K3 = clean and K2/K4 = noisy by filename convention; this never enters inference.
- All available records are `development`; no `challenge` or `hidden` split was supplied.
- Gold comes from `data/Skript_mit_Sollbewertung/<dialogue>.txt`, read only after inference for scoring.
- The files do not establish whether gold was audio-verified rather than script-derived; `gold_source` is recorded, but audio-verified gold is **not** claimed.

Questions for dataset authors: meaning of `R01`–`R11` and `M`; who authored/validated gold; whether gold is script-only or audio-verified; whether clean/noisy is contractual; whether a hidden/challenge split exists. No undocumented identifier has been interpreted.

## Current audio baseline: 42 recordings / 21 dialogues

Source: `runs/eval-20261010-164409-audio/{summary,manifest}.json`.

| Gold \ prediction | alarm | review | no_alert |
| --- | ---: | ---: | ---: |
| alarm | 16 | 0 | 0 |
| review | 0 | 6 | 0 |
| no_alert | 0 | 0 | 20 |

| Measure | Value |
| --- | ---: |
| Accuracy, headline (unresolved included) | 42/42 = 1.000 |
| Accuracy, available predictions only | 42/42 = 1.000 |
| Alarm precision / recall | 16/16 / 16/16 |
| False alarms, all gold non-alarm | 0/26 |
| alarm → review | 0/16 |
| alarm → no_alert | 0/16 |
| review → alarm / no_alert | 0/6 / 0/6 |
| Alarm triage coverage (alarm or review) | 16/16 |
| Review rate | 6/42 = 14.29% |
| Review reason | `missing_policy_fact`: 6; technical reviews: 0 |

`alarm → review` is not a true-positive alarm. Triage coverage is separate.

These are **recording-level** counts: the audio run has 42 recordings, not 42 independent dialogues. At the frozen balanced threshold, every clean/noisy pair has the same label, so the equivalent dialogue-level distribution is 8 alarm, 3 review and 10 no_alert — the distribution reported in `PLAN.md`.

Operational completion is `expected=42`, `processed=42`, `with_prediction=42`, `unresolved=0`, `technical_failure=0`. ASR failures, extraction/schema failures, incomplete extractions and grounding issues are all 0. A technical fallback remains `review` in the matrix and is also a technical failure; an ASR failure with no transcript is `unresolved`, never `no_alert`.

### Clean/noisy and levels

There are 21 complete pairs: clean 21/21 and noisy 21/21 correct; 21 dialogues have both variants correct; 0 prediction-discordant pairs, 0 both-wrong pairs and 0 incomplete pairs. This is paired development-set evidence, not 42 independent scenarios and not proof that production noise has no effect.

Stufe 1: 12/12; Stufe 2: 24/24; Stufe 3: 6/6. Family metrics are omitted because multi-family counting semantics are not documented by the gold.

### Threshold comparison on identical cached facts

| Preset | alarm | review | no_alert | Delta from balanced |
| --- | ---: | ---: | ---: | --- |
| Sensitive (0.50) | 16 | 6 | 20 | none |
| **Balanced (0.60, frozen primary)** | 16 | 6 | 20 | — |
| Conservative (0.75) | 15 | 7 | 20 | `Stufe1_D02-K2`: alarm → review |

This reruns deterministic decision only over cached facts; it makes no ASR/chat calls. It is development sensitivity analysis, not post-hoc hidden-set selection.

## Script baselines and known error

| Mode | Run | Accuracy | Review load | Technical failures | Known error |
| --- | --- | ---: | ---: | ---: | --- |
| Speaker labels | `runs/eval-20261010-152023-speakers/` | 21/21 | 3/21 | 0 | none |
| No speaker labels | `runs/eval-20261010-152023-nospeakers/` | 20/21 | 4/21 | 1 | `Stufe2_C06`: gold alarm → `review/technical_uncertainty` |

C06 is not an alarm true positive: alarm recall 7/8, alarm→review 1/8, triage coverage 8/8. Strict quote grounding rejects `own_trade_request`; uncertainty stays review. It appears in `runs/eval-20261010-160120-nospeakers/errors.json` and was not selectively rerun until it disappeared.

## Evidence and human audit

Automatic grounding verifies quote/transcript/timestamps. It does **not** prove semantic support for a predicate or transcript fidelity to audio. These checks stay separate:

1. Automatic grounding: implemented and reported separately.
2. Semantic evidence correctness: needs a reviewer.
3. Audio fidelity: needs listening to the recording.

`runs/eval-20261010-164409-audio/human-audit-checklist.md` is intentionally unfilled. It selects alarm, no-alert, review and noisy samples; C06 is available in its run's error list. Audio-listened, evidence/predicate consistency, error type, reviewer and date are pending. No human/audio audit is claimed.

### Canonical-result traceability and empty facts

The current release JSON has `schema_version: "callguard.canonical-result.v1"` and links each result to a run ID, policy version, recording hash, evaluated rules, deterministic explanation, review uncertainty and clamped evidence-clip bounds. Its post-decision `rationale` is template-only: it points to supported conditions for an alarm, an explicitly excluded condition or no candidate event for no-alert, and a missing fact or technical blocker for review. It never asks a model for a free explanation or changes cached facts. The precise compatibility contract and structural examples for alarm, review and no-alert live in `CANONICAL_RESULT_SCHEMA.md`.

Clean and noisy are paired recordings of the same dialogue: clean is `K1`/`K3`, noisy is `K2`/`K4`, by the supplied filename convention. They are not independent scenarios. `Stufe1_D03-K1` contains a trade event whose three decisive facts are explicitly excluded; `Stufe1_D03-K2` has no extracted candidate event at all (`events: []`, `evaluated_rules: []`). Its gold-aligned `no_alert` is therefore an absence of extracted policy facts, not evidence that the noisy recording is safe. The dashboard states this explicitly rather than rendering an empty “What we know” panel.

## Performance, cost and reproduction

The audio candidate had 42/42 ASR and extraction cache hits. ASR mean 0.028 s, decision/output mean 0.005 s and audio→result mean 0.009 s are cache-path timings; measured uncached extraction is `n/a`. The 0.279 s batch wall clock is not divided by 42 to represent per-call latency. Provider cost is unavailable in release artefacts; no zero-cost claim is made.

```bash
python3 -m unittest discover -s tests
python3 -m callguard.pipeline eval --audio --workers 6
python3 -m callguard.pipeline eval --workers 6
python3 -m callguard.pipeline eval --no-speakers --workers 6
```

Each evaluation writes canonical JSON, `manifest.json`, `summary.json`, `recordings.csv`, `errors.json` and `human-audit-checklist.md`. Cache validity is defined by the manifest profile/model/prompt/messages/schema contract.

## Release blockers / human work

- The candidate is not self-hostable compliant. A self-hosted profile requires smoke plus full re-evaluation.
- No hidden or independent challenge result exists.
- Dataset-author confirmation of gold provenance and `R01`–`R11` / `M` is pending.
- Human semantic/audio audit is pending.
- No uncached full-audio latency or measured provider cost is available.
