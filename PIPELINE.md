# CallGuard pipeline

CallGuard turns a Swiss German bank-call recording into an auditable **alarm**, **review**, or **no alert** result. The dashboard-ready run committed on this branch is `runs/eval-20261011-070834-audio`; it contains 42 authentic processed calls and is the default source for `python3 -m callguard.ui`.

The diagrams are in [PIPELINE_FLOW.md](PIPELINE_FLOW.md). For a clean-clone launch procedure, use [SETUP.md](SETUP.md).

## Processing model

```text
WAV -> ASR segments -> structured fact extraction -> deterministic decision -> canonical result
                                                    -> evidence clips and dashboard
```

1. `callguard/asr.py` transcribes WAV audio into timestamped segments. ASR results are keyed by the source content and profile and may be cached locally.
2. `callguard/extract.py` sends the selected transcript view to a chat model. It returns structured policy facts with condition state (`true`, `false`, or `unknown`) and quoted segment evidence.
3. `callguard/decide.py` is deterministic. It validates each quote against the cited segment, applies the policy rules, evaluates ASR-quality escalation, and emits the final label and reasons.
4. `callguard/ui.py` renders a persisted run. It does not call an ASR or chat model while a user browses the dashboard.

The model reports evidence; code makes the decision. Keyword matches are highlights only and never replace extraction or change a classification.

## Policy outcomes

The configured policy families are `trade`, `access`, `disclosure`, `documentation`, `splitting`, and `numbers`. A family can be disabled in `config/policies.json`.

- A fully supported event can become an **alarm**.
- A missing fact, an unresolved required speaker role, a grounding problem, or evidence below the configured ASR-quality threshold becomes a **review** with an explicit reason.
- An explicitly excluded event becomes **no alert**.
- The call label is the highest-severity result across its events.

Review settings provide three fixed ASR-quality thresholds: `0.6` (default), `0.7`, and `0.8`. They are an ASR-quality heuristic, not a fraud probability. Switching a value recomputes the deterministic decision over saved evidence; it does not call a model.

## Published dashboard run

`runs/eval-20261011-070834-audio` is self-contained:

- 42 flat canonical call results used by the dashboard;
- per-call ASR, extraction, decision, status, and model-input artifacts;
- 42 run-owned WAV files, so playback does not depend on `data/Audio/`;
- `manifest.json`, `summary.json`, `artifact_index.json`, and `checksums.json` metadata.

`runs/_default_run.txt` points to this directory. The UI recognizes genuine call results by their `label` and `events` fields and does not treat metadata JSON files as calls.

The source-data workspace can contain 48 WAVs, including six `Stufe*-M*.wav` recordings. The published run intentionally selects the 42 authentic `K1`/`K2`/`K3`/`K4` calls only.

## Configured inference profiles

The current self-hosted path uses:

| Task | Profile | Required configuration |
|---|---|---|
| Chat extraction | `qwen3_fast` | `QWEN_URL`, `QWEN_KEY`, optional `QWEN_MODEL` |
| Speech-to-text | `whisper_local` | `WHISPER_URL`, optional `WHISPER_MODEL` |

Both profiles are defined in `config/models.json`. `swiss_whisper_local` is not defined there and must not be used in commands or setup instructions. Other configured profiles are available for development, but the default dashboard needs none of them because it reads the committed run.

## Commands

```sh
# Open the committed 42-call dashboard; no model server or API key required.
python3 -m callguard.ui

# Inspect the active profiles before a new run.
python3 -m callguard.models

# Process one recording.
python3 -m callguard.pipeline audio data/Audio/Stufe1_D02-K1.wav

# Process every WAV in data/Audio (not the curated published 42-call selection).
python3 -m callguard.pipeline eval --audio --require-self-hostable --workers 2

# Run the script regression subset.
python3 -m callguard.pipeline eval --smoke --workers 1
```

For the exact first-run procedure and the rules for publishing a new populated dashboard, see [SETUP.md](SETUP.md).
