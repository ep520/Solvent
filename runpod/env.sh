# Source in every new terminal on the pod:   source runpod/env.sh
V=/workspace/callguard-env
if [ -x "$V/asr/bin/python" ]; then                        # CallGuard itself is standard-library Python; it runs in the
  source "$V/asr/bin/activate"                             # Whisper venv so transcription happens in the same process
  export LD_LIBRARY_PATH="$(python -c 'import os, nvidia.cublas, nvidia.cudnn; print(":".join(os.path.join(p.__path__[0], "lib") for p in (nvidia.cublas, nvidia.cudnn)))' 2>/dev/null)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
export HF_HOME="${HF_HOME:-/workspace/hf}"
export CALLGUARD_CHAT="${CALLGUARD_CHAT:-qwen3}"          # qwen3 (thinking) | qwen3_fast (hybrid models, thinking off)
export CALLGUARD_ASR="${CALLGUARD_ASR:-faster_whisper}"   # Whisper in-process on the pod's GPU (self-hosted)
export QWEN_URL="${QWEN_URL:-http://127.0.0.1:8000/v1}"
export QWEN_KEY="${VLLM_API_KEY:-${QWEN_KEY:-local}}"     # vLLM ignores it unless started with --api-key
export QWEN_MODEL="${QWEN_MODEL:-$(cat /workspace/vllm_model.txt 2>/dev/null || echo Qwen/Qwen3-30B-A3B-Thinking-2507)}"
echo "CALLGUARD_CHAT=$CALLGUARD_CHAT  CALLGUARD_ASR=$CALLGUARD_ASR  QWEN_MODEL=$QWEN_MODEL  python=$(command -v python3)"
