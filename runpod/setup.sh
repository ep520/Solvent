#!/usr/bin/env bash
# Install and start self-hosted Qwen3 (vLLM) + Whisper (faster-whisper) on a RunPod GPU pod for CallGuard.
# Follows the organizer kit's tested recipe (A40, Python 3.12, Torch 2.8/CUDA 12.8): separate venvs on /workspace,
# vLLM 0.10.2 via uv, faster-whisper 1.2.1 with av==16.0.1 and its own cuBLAS/cuDNN.
#   bash runpod/setup.sh                        # picks the largest Qwen3 that fits the GPU
#   MODEL=Qwen/Qwen3-14B-AWQ bash runpod/setup.sh
#   VLLM_API_KEY=secret bash runpod/setup.sh    # only if the API is reachable from outside the pod
# vLLM gets 80% of the GPU memory (GPU_UTIL); the rest is for Whisper large-v3 (~4 GB).
set -euo pipefail
cd "$(dirname "$0")/.."                                   # -> repo root
mountpoint -q /workspace || echo "!! /workspace is not a mounted volume: models and venvs will not survive a restart"

V=/workspace/callguard-env                                # venvs + caches on the persistent volume
export HF_HOME="${HF_HOME:-/workspace/hf}"
export UV_CACHE_DIR="$V/cache/uv"
mkdir -p "$V" "$HF_HOME" "$UV_CACHE_DIR"

# CallGuard reads data/ next to the code. Keep the real data on the volume and link it.
if [ ! -e data ] && [ -d /workspace/data ]; then ln -s /workspace/data data; echo "== linked data -> /workspace/data"; fi
[ -d data/Audio ] || echo "!! data/Audio not found: upload the WAVs before running run_dashboard.sh"

if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c "import ensurepip" 2>/dev/null; then
  apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
fi

if [ ! -x "$V/vllm/bin/vllm" ]; then
  echo "== installing vLLM ${VLLM_VERSION:-0.10.2} (5-10 min the first time)"
  python3 -m venv "$V/tools" && "$V/tools/bin/python" -m pip install -q uv==0.12.24
  python3 -m venv "$V/vllm"
  "$V/tools/bin/uv" pip install --python "$V/vllm/bin/python" --torch-backend=auto \
      "vllm==${VLLM_VERSION:-0.10.2}" "transformers==${TRANSFORMERS_VERSION:-4.55.4}"
fi
# RunPod images set HF_HUB_ENABLE_HF_TRANSFER=1; downloads then fail unless hf_transfer is installed in each venv.
"$V/vllm/bin/python" -c "import hf_transfer" 2>/dev/null || "$V/vllm/bin/python" -m pip install -q hf_transfer
if [ ! -x "$V/asr/bin/python" ] || ! "$V/asr/bin/python" -c "import faster_whisper" 2>/dev/null; then
  echo "== installing faster-whisper (pinned as in the organizer kit)"
  python3 -m venv "$V/asr"
  "$V/asr/bin/python" -m pip install -q --upgrade pip
  "$V/asr/bin/python" -m pip install -q faster-whisper==1.2.1 ctranslate2==4.8.2 av==16.0.1 \
      nvidia-cublas-cu12==12.9.2.10 nvidia-cudnn-cu12==9.27.0.42
fi
"$V/asr/bin/python" -c "import hf_transfer" 2>/dev/null || "$V/asr/bin/python" -m pip install -q hf_transfer

MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.' || echo 0)
echo "== GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1), ${MEM} MiB, compute capability ${CC}"
if [ -z "${MODEL:-}" ]; then
  if   [ "$MEM" -ge 70000 ]; then MODEL="Qwen/Qwen3-30B-A3B-Thinking-2507"                 # A100/H100 80GB
  elif [ "$MEM" -ge 40000 ] && [ "${CC:-0}" -ge 89 ]; then MODEL="Qwen/Qwen3-30B-A3B-Thinking-2507-FP8"  # L40S/H100: FP8 hardware
  elif [ "$MEM" -ge 40000 ]; then MODEL="Qwen/Qwen3-32B-AWQ"                               # A40/A6000 48GB: no FP8 on Ampere
  elif [ "$MEM" -ge 22000 ]; then MODEL="Qwen/Qwen3-14B-AWQ"                               # RTX 4090/3090 24GB
  else                            MODEL="Qwen/Qwen3-8B-AWQ"; fi                            # 16GB cards
fi
echo "$MODEL" > /workspace/vllm_model.txt
echo "== model: $MODEL"

if pgrep -f "vllm serve" >/dev/null; then
  echo "== vLLM already running (see /workspace/vllm.log)"
else
  API_ARGS=()
  [ -n "${VLLM_API_KEY:-}" ] && API_ARGS=(--api-key "$VLLM_API_KEY")
  nohup "$V/vllm/bin/vllm" serve "$MODEL" --host 127.0.0.1 --port 8000 \
        --max-model-len 40960 --gpu-memory-utilization "${GPU_UTIL:-0.80}" \
        --reasoning-parser qwen3 "${API_ARGS[@]}" > /workspace/vllm.log 2>&1 &
  echo "== starting vLLM (first start downloads the model: 5-15 min) - log: /workspace/vllm.log"
fi

AUTH=()
[ -n "${VLLM_API_KEY:-}" ] && AUTH=(-H "Authorization: Bearer $VLLM_API_KEY")
for i in $(seq 1 180); do
  if curl -sf "${AUTH[@]}" http://127.0.0.1:8000/v1/models >/dev/null; then
    echo "== vLLM ready on 127.0.0.1:8000"; break
  fi
  if ! pgrep -f "vllm serve" >/dev/null; then
    echo "!! vLLM stopped - last log lines:"; tail -30 /workspace/vllm.log; exit 1
  fi
  sleep 10
done
curl -sf "${AUTH[@]}" http://127.0.0.1:8000/v1/models >/dev/null || { echo "!! not ready after 30 min"; tail -30 /workspace/vllm.log; exit 1; }
echo "== next: bash runpod/run_dashboard.sh --only D02,C05   (then without --only for all calls)"
