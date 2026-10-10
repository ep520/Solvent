# Final release acceptance

This checklist is fail-closed. A cached replay is useful as a backup, but it does not prove that the final
self-hosted chat and ASR services are running.

## Current status

| Gate | Status | Evidence / remaining action |
| --- | --- | --- |
| Self-hosted chat and ASR live, no cloud fallback | **BLOCKED** | No self-hosted profile variables or reachable local endpoints were present during the 2026-10-10 audit. Defaults remain `claude_code` and `openai`; do not use them for acceptance. `python3 -m callguard.models --probe --require-self-hostable` now verifies `/models` and exact model IDs without consulting caches. |
| Smoke and full evaluation on final candidate | **BLOCKED** | Must run with the exact final self-hosted profiles. `scripts/release_acceptance.sh` performs live probe, tests, audio smoke, full audio evaluation and freeze in that order. |
| Cache compatible with final model and full transcript | **READY / final run pending** | ASR cache keys include audio hash, profile/model and verbatim constraint. Extraction keys include profile/model, prompt, complete rendered transcript, schema/policies and feedback examples. `run_segments()` always uses `view=None`. Old `extract-6` replay is not the final `extract-7` cache. |
| New WAV through ingest to UI | **MOCK VERIFIED / real run pending** | Offline tests cover move, processing, quarantine and UI cache-only reading. Acceptance still requires a genuinely new WAV with the final services: place it in `data/Inbox/`, run ingest once, then verify it appears in `/api/data` and plays its clip. |
| Human listening: alarm, review, borderline | **PENDING HUMAN** | Fill the final run's `human-audit-checklist.md`; confirm ASR wording, semantic predicate, speaker role, timestamp and ±10 s clip. Minimum: one alarm, one semantic review, one harmless keyword/borderline call, and a noisy take. |
| Threshold described and measured | **IMPLEMENTED / final measurement pending** | Threshold is `exp(avg_logprob)` evidence quality, not fraud probability. Development replay showed little separation; report final-candidate sensitivity without selecting the threshold on hidden labels. |
| Keywords and checks configurable without code | **VERIFIED** | Dashboard CRUD validates and atomically saves keyword and policy configuration. Family `enabled` flags alter prompt/schema; keywords affect highlights only under the full-transcript MVP. |
| Report current, errors included, hidden without retuning | **BLOCKED on final run** | Publish final `summary.json`, `errors.json`, manifest and listening checklist. Freeze configuration before hidden; after labels are revealed, report once and make no prompt/policy/model/threshold changes. |
| Four demo scenes and declared replay backup | **RUNBOOK READY / rehearsal pending** | See `DEMO_RUNBOOK.md`. Historical replay is explicitly non-self-hosted and may demonstrate UI only; it is not final-candidate evidence. |
| Freeze code, models, prompt, policy and threshold | **IMPLEMENTED / final clean run pending** | Run manifest records commit/dirty state, source hashes, exact chat/ASR profiles and models, prompt version, Qwen generation settings, policy hash/version, presets/default threshold, keywords, dataset hashes and cache contract. `freeze --audio` now freezes actual audio extractions. Acceptance requires a clean commit. |

## Automated gate

Configure the final endpoints, commit all intended code, then run:

```sh
export CALLGUARD_CHAT=qwen3
export CALLGUARD_ASR=whisper_local
export QWEN_URL=http://localhost:8000/v1
export QWEN_KEY=local
export QWEN_MODEL=Qwen/Qwen3-14B-AWQ
export WHISPER_URL=http://localhost:8001/v1
export WHISPER_MODEL=Systran/faster-whisper-large-v3
bash scripts/release_acceptance.sh
```

The gate intentionally stops on a dirty tree, missing endpoint, wrong served model, non-self-hostable profile,
test failure, smoke failure, full-run failure or missing compatible extraction cache.

## Manual completion record

Record the final run directory, commit, chat model, ASR model, smoke/full counts, error count, listening reviewer/date,
new-WAV ID, hidden run ID and demo rehearsal date here. Until every field is filled, release acceptance remains open.
