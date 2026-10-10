#!/usr/bin/env bash
# Transcribe every WAV in data/Audio, run snippets + Qwen3 + decision, then serve the dashboard.
#   bash runpod/run_dashboard.sh              # all calls, then dashboard on port 8090
#   bash runpod/run_dashboard.sh --only D02   # a few calls first
# SSH-only pod (organizer kit): open an SSH tunnel -L 8090:127.0.0.1:8090 and browse http://127.0.0.1:8090
# Pod with an exposed HTTP port: UI_HOST=0.0.0.0 bash runpod/run_dashboard.sh, then https://<POD_ID>-8090.proxy.runpod.net
set -euo pipefail
cd "$(dirname "$0")/.."
source runpod/env.sh
python3 -m callguard.pipeline prepare --require-self-hostable --workers "${WORKERS:-4}" "$@" || true   # failed calls stay pending
rm -f /workspace/dashboard_results.zip
python3 -m zipfile -c /workspace/dashboard_results.zip cache data/Transcriptions && \
  echo "== results packed: /workspace/dashboard_results.zip (download it to view the dashboard on your PC)"
echo "== dashboard on ${UI_HOST:-127.0.0.1}:8090 (Ctrl+C to stop)"
exec python3 -m callguard.ui --host "${UI_HOST:-127.0.0.1}" --port 8090
