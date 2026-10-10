# Source in every new terminal on the pod:   source runpod/env.sh
export CALLGUARD_CHAT="${CALLGUARD_CHAT:-qwen3}"          # qwen3 (thinking) | qwen3_fast (hybrid models, thinking off)
export QWEN_URL="${QWEN_URL:-http://localhost:8000/v1}"
export QWEN_KEY="${VLLM_API_KEY:-${QWEN_KEY:-local}}"     # vLLM ignores it unless started with --api-key
export QWEN_MODEL="${QWEN_MODEL:-$(cat /workspace/vllm_model.txt 2>/dev/null || echo Qwen/Qwen3-30B-A3B-Thinking-2507)}"
echo "CALLGUARD_CHAT=$CALLGUARD_CHAT  QWEN_MODEL=$QWEN_MODEL  QWEN_URL=$QWEN_URL"
