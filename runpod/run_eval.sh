#!/usr/bin/env bash
# Evaluate CallGuard with self-hosted Qwen3 against the expected assessments (data/Skript_mit_Sollbewertung).
#   bash runpod/run_eval.sh                 # all transcripts
#   bash runpod/run_eval.sh --smoke         # the 5-call regression set
#   bash runpod/run_eval.sh --only C05,D09
#   bash runpod/run_eval.sh --audio         # the WAVs (needs a self-hosted ASR profile, see README)
# Extra arguments go to `python3 -m callguard.pipeline eval`.
set -euo pipefail
cd "$(dirname "$0")/.."
source runpod/env.sh
python3 -m callguard.pipeline eval --require-self-hostable --workers "${WORKERS:-8}" "$@"
