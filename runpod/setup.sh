#!/usr/bin/env bash
# Start self-hosted Qwen3 (vLLM) on a RunPod GPU pod for CallGuard.
#   bash runpod/setup.sh                        # picks the largest Qwen3 that fits the GPU
#   MODEL=Qwen/Qwen3-8B-AWQ bash runpod/setup.sh
#   VLLM_API_KEY=secret bash runpod/setup.sh    # needed only if you call the pod from your laptop
set -euo pipefail
cd "$(dirname "$0")/.."                                   # -> repo root

export HF_HOME="${HF_HOME:-/workspace/hf}"                # model cache on the persistent volume
mkdir -p "$HF_HOME" /workspace

# CallGuard reads data/ next to the code. Keep the real data on the volume and link it.
if [ ! -e data ] && [ -d /workspace/data ]; then ln -s /workspace/data data; echo "== linked data -> /workspace/data"; fi

echo "== installing vLLM (3-5 min the first time; CallGuard itself needs only the standard library)"
pip install -q --upgrade pip
pip install -q vllm

MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
echo "== GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1), ${MEM} MiB"
if [ -z "${MODEL:-}" ]; then
  if   [ "$MEM" -ge 70000 ]; then MODEL="Qwen/Qwen3-30B-A3B-Thinking-2507"        # A100/H100 80GB
  elif [ "$MEM" -ge 40000 ]; then MODEL="Qwen/Qwen3-30B-A3B-Thinking-2507-FP8"    # L40S/A6000 48GB
  elif [ "$MEM" -ge 22000 ]; then MODEL="Qwen/Qwen3-14B-AWQ"                      # RTX 4090/3090 24GB
  else                            MODEL="Qwen/Qwen3-8B-AWQ"; fi                   # 16GB cards
fi
echo "$MODEL" > /workspace/vllm_model.txt
echo "== model: $MODEL"

if pgrep -f "vllm serve" >/dev/null; then
  echo "== vLLM already running (see /workspace/vllm.log)"
else
  API_ARGS=()
  [ -n "${VLLM_API_KEY:-}" ] && API_ARGS=(--api-key "$VLLM_API_KEY")
  nohup vllm serve "$MODEL" --host 0.0.0.0 --port 8000 \
        --max-model-len 40960 --gpu-memory-utilization 0.90 \
        --reasoning-parser qwen3 "${API_ARGS[@]}" > /workspace/vllm.log 2>&1 &
  echo "== starting vLLM (first start downloads the model: 2-10 min) - log: /workspace/vllm.log"
fi

AUTH=()
[ -n "${VLLM_API_KEY:-}" ] && AUTH=(-H "Authorization: Bearer $VLLM_API_KEY")
for i in $(seq 1 180); do
  if curl -sf "${AUTH[@]}" http://localhost:8000/v1/models >/dev/null; then
    echo "== vLLM ready on port 8000"; break
  fi
  if ! pgrep -f "vllm serve" >/dev/null; then
    echo "!! vLLM stopped - last log lines:"; tail -30 /workspace/vllm.log; exit 1
  fi
  sleep 10
done
curl -sf "${AUTH[@]}" http://localhost:8000/v1/models >/dev/null || { echo "!! not ready after 30 min"; tail -30 /workspace/vllm.log; exit 1; }
echo "== next: source runpod/env.sh && bash runpod/run_eval.sh --smoke"
