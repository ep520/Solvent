# CallGuard setup

This branch ships with a complete, self-contained dashboard run. A fresh clone can display all 42 processed authentic calls, their evidence, and their original audio without an API key, a model server, a cache, or the untracked `data/` directory.

## Fast path: open the populated dashboard

Prerequisites: Git, Python 3.10 or later, and a modern browser.

```sh
git clone --branch evaluation-prep https://github.com/ep520/Solvent.git
cd Solvent
python3 -m callguard.ui
```

Open <http://127.0.0.1:8090>. The server reads `runs/_default_run.txt`, which points to `runs/eval-20261011-070834-audio`. That committed run contains 42 canonical results, their transcripts, evidence, model input, checksums, and run-owned WAV files. No inference is performed when opening the dashboard.

If port 8090 is occupied, use another port:

```sh
python3 -m callguard.ui --port 8091
```

## What is included in the default run

The repository directory may contain 48 WAV files when the private source data is present. The published dashboard run intentionally includes only the 42 authentic calls: all `*-K1`, `*-K2`, `*-K3`, and `*-K4` recordings. It excludes the six `Stufe*-M*.wav` recordings. The dashboard loads the committed run rather than scanning `data/Audio`, so a fresh clone always shows exactly those 42 processed calls.

## Run inference again (optional)

Re-running inference is not needed for the dashboard demo. It requires access to self-hosted, OpenAI-compatible chat and ASR services, plus the private source recordings in `data/Audio/`.

1. Install the dependencies required by your model servers and local tooling:

   ```sh
   python3 -m pip install -r requirements.txt
   ```

2. Start an OpenAI-compatible Qwen server and a separate Whisper-compatible ASR server. Configure the profiles that actually exist in `config/models.json`:

   ```sh
   export CALLGUARD_CHAT=qwen3_fast
   export QWEN_URL=http://127.0.0.1:8000/v1
   export QWEN_KEY=local
   export QWEN_MODEL=Qwen/Qwen3-14B-AWQ

   export CALLGUARD_ASR=whisper_local
   export WHISPER_URL=http://127.0.0.1:8001/v1
   # Optional: set this when the ASR server exposes a different model name.
   # export WHISPER_MODEL=your-swiss-german-whisper-model
   ```

3. Check the configured services, then process audio:

   ```sh
   python3 -m callguard.models
   python3 -m callguard.pipeline eval --audio --require-self-hostable --workers 2
   ```

`--audio` processes every WAV visible in `data/Audio`. If that directory contains the six `M-*` recordings as well, select the desired files with `--only`; do not assume that the command recreates the published 42-call selection. A pipeline run writes a new directory under `runs/`. To make one the dashboard default, write its relative path to `runs/_default_run.txt` and commit both the pointer and the complete run artifacts.

## Validation with private source data

```sh
python3 -m unittest discover -s tests
python3 -m callguard.pipeline eval --smoke --workers 1
```

These commands require the private source-data package. The smoke command uses the script regression set and does not create the published dashboard run. They are not needed to open the committed dashboard after cloning.

## Troubleshooting

- **The dashboard is empty:** verify that `runs/_default_run.txt` names an existing run directory.
- **A new audio run fails with an unknown profile:** use `whisper_local`, not `swiss_whisper_local`; the latter is not a configured profile in this branch.
- **The dashboard cannot start:** check whether port 8090 is in use, then choose `--port 8091`.
