# CallGuard setup

## Canonical working directory

Use the repository root (`.`), on branch `local-pipeline`.
The sibling `../Solvent` is another clone at the same commit, not a second source of truth. This
guide does not delete it; remove or archive it only after confirming that it has no local work to keep.

```sh
# Run these commands from the directory containing this file.
git switch local-pipeline
python3 -m unittest discover -s tests
```

## Local run

Create `.env` from `.env.example` if it does not exist and configure either OpenAI/Claude or a
self-hosted chat and ASR profile. Then export transcripts before opening the dashboard:

```sh
python3 -m callguard.asr data/Audio --profile openai
python3 -m callguard.pipeline eval --audio --smoke
python3 -m callguard.ui
```

Open <http://127.0.0.1:8090>. For continuous intake, use `python3 -m callguard.ingest`; it moves WAVs
from `data/Inbox/`, exports transcripts and updates the same cache/output flow.
