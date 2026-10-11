# CallGuard Setup and Installation

## Prerequisites

- **OS:** Linux (tested on Ubuntu 24.04)
- **Python:** 3.10 or later
- **GPU:** NVIDIA GPU with CUDA 12.8+ (for self-hosted models; optional for CLI-only evaluation)
- **Storage:** ~50 GB for models (Qwen3-32B, Whisper checkpoints)
- **RAM:** 32+ GB for concurrent inference

## Installation

### 1. Clone the repository and install dependencies

```bash
git clone https://github.com/ep520/Solvent.git
cd Solvent
pip install -r requirements.txt
```

### 2. Configure environment

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` and set:

- `OPENAI_API_KEY` (if using cloud Whisper/Chat; optional for self-hosted)
- `HF_HOME` (HuggingFace cache directory, default: `~/.cache/huggingface`)
- `CALLGUARD_ASR` (ASR profile: `openai`, `whisper_local`, `swiss_whisper_local`)
- `CALLGUARD_CHAT` (Chat profile: `claude_code`, `qwen3`, `qwen3_fast`, `ollama_qwen3`)

Example for self-hosted Qwen3 + Whisper:

```bash
export CALLGUARD_ASR=swiss_whisper_local
export CALLGUARD_CHAT=qwen3
export WHISPER_URL=http://127.0.0.1:8002/v1
export QWEN_URL=http://127.0.0.1:8000/v1
```

### 3. Download models (self-hosted only)

Models are downloaded automatically on first use via HuggingFace. Pre-download if desired:

```bash
# ASR model (faster-whisper format)
python3 -c "from faster_whisper import WhisperModel; \
  WhisperModel('jayr23/whisper-large-v3-turbo-swiss-german-ct2', \
  device='cuda', compute_type='float16')"

# Chat model (requires vLLM)
python3 -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-32B-AWQ \
  --quantization awq \
  --dtype auto
```

### 4. Start services (self-hosted)

In separate terminals:

**ASR Server** (faster-whisper):

```bash
python3 runpod/asr_server.py --host 127.0.0.1 --port 8002
```

**Chat Server** (vLLM):

```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-32B-AWQ \
  --quantization awq \
  --dtype auto \
  --port 8000
```

### 5. Run the application

**Dashboard (web UI):**

```bash
python3 -m callguard.ui
```

Open <http://127.0.0.1:8090>

**Batch evaluation:**

```bash
python3 -m callguard.pipeline eval --audio --workers 6
```

**Minimal example (single call):**

```bash
python3 -m callguard.pipeline audio data/Audio/Stufe1_D02-K1.wav
```

## Troubleshooting

### "Model not found" error

Ensure HF_HOME points to correct directory and HuggingFace token is set (for private models):

```bash
export HF_HOME=/path/to/cache
export HF_TOKEN=hf_xxxx...
```

### ASR/Chat server not responding

Verify endpoints and ports:

```bash
python3 -m callguard.models --probe --require-self-hostable
```

### Tests fail with cache errors

Clear the evaluation cache:

```bash
rm -rf cache/
python3 -m unittest discover -s tests
```

## Smoke Test

Run a quick verification:

```bash
python3 -m callguard.pipeline eval --smoke --workers 5
```

This processes 5 test calls using cached transcripts (no ASR calls).

## Documentation

- **Pipeline design & results:** [`PIPELINE.md`](PIPELINE.md)
- **Evaluation methodology:** [`EVALUATION.md`](EVALUATION.md)
- **Data & split methodology:** [`DATASET.md`](DATASET.md)
- **Output schema:** [`CANONICAL_RESULT_SCHEMA.md`](CANONICAL_RESULT_SCHEMA.md)
