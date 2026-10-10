# Running CallGuard with self-hosted Qwen3 on RunPod

## 1. Create the pod (runpod.io → Pods → Deploy)

| GPU | Model picked by `setup.sh` | Profile |
|---|---|---|
| A100 80GB / H100 | `Qwen/Qwen3-30B-A3B-Thinking-2507` | `qwen3` (best quality) |
| L40S / A6000 48GB | `Qwen/Qwen3-30B-A3B-Thinking-2507-FP8` | `qwen3` (best value) |
| RTX 4090 / 3090 24GB | `Qwen/Qwen3-14B-AWQ` | `qwen3` or `qwen3_fast` |

- Template: **RunPod PyTorch** (CUDA 12). **Volume disk 100 GB** at `/workspace` (models, data, caches survive restarts).
- Expose HTTP port **8000** only if you want to call the model from your laptop.

## 2. Code and data
In **Connect → Web Terminal**:
```
cd /workspace && git clone -b dev https://github.com/ep520/Solvent.git && cd Solvent
```
Upload your `data` folder (zipped) into `/workspace` and unzip it to `/workspace/data`
(`Transkript/`, `Skript_mit_Sollbewertung/`, `Stichwortliste.json`, optionally `Audio/`).
`setup.sh` links it as `Solvent/data`. The data never goes into git.

## 3. Start Qwen3
```
bash runpod/setup.sh          # wait for "vLLM ready"; first start downloads the model (2-10 min)
```

## 4. Evaluate
```
bash runpod/run_eval.sh --smoke             # 5-call regression set
bash runpod/run_eval.sh                     # all transcripts → runs/eval-*/summary.json + manifest.json
bash runpod/run_eval.sh --only C05,D09
CALLGUARD_CHAT=qwen3_fast bash runpod/run_eval.sh   # hybrid models only: thinking off, much faster
```
Extractions are cached in `cache/extract/` (keyed by profile, model, prompt version, transcript and policies),
so re-running with another threshold costs nothing. The manifest records that the run was self-hostable.

## 5. Dashboard from the pod
```
source runpod/env.sh && python3 -m callguard.ui --host 0.0.0.0 --port 8090
```
Expose port 8090 and open `https://<POD_ID>-8090.proxy.runpod.net`. The dashboard lists calls from
`data/Audio/` with transcripts in `data/Transcriptions/` (run with `--audio` and a self-hosted ASR profile).

## 6. Optional: use the pod's model from your laptop
On the pod: `VLLM_API_KEY=choose-a-secret bash runpod/setup.sh` (expose port 8000). On the laptop (PowerShell):
```
$env:CALLGUARD_CHAT="qwen3"
$env:QWEN_URL="https://<POD_ID>-8000.proxy.runpod.net/v1"
$env:QWEN_KEY="choose-a-secret"
$env:QWEN_MODEL="<model from /workspace/vllm_model.txt>"
python -m callguard.pipeline text data\Transkript\Stufe2_C05.txt
```

## Troubleshooting
- `vLLM stopped` during setup: read `/workspace/vllm.log`. Out of memory → `MODEL=Qwen/Qwen3-8B-AWQ bash runpod/setup.sh`.
- `call needs about N tokens, context is 32768`: raise `context_tokens` of the profile in `config/models.json`
  (vLLM is started with 40960).
- Errors about `response_format` / JSON schema with reasoning: set `"json_mode": "json_object"` for the profile.
- Replies cut off: thinking uses output tokens; raise `max_output_tokens` (the `qwen3` profile has 12288).
- **Stop the pod when done**: GPU pods bill per minute. `/workspace` keeps everything.
