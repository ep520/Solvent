#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

: "${CALLGUARD_CHAT:?Set CALLGUARD_CHAT to the final self-hosted chat profile}"
: "${CALLGUARD_ASR:?Set CALLGUARD_ASR to the final self-hosted ASR profile}"

workers="${CALLGUARD_ACCEPTANCE_WORKERS:-4}"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "release acceptance requires a clean working tree" >&2
  exit 1
fi

python3 -m callguard.models --probe --require-self-hostable
python3 -m unittest discover -s tests
python3 -m callguard.pipeline eval --audio --smoke --require-self-hostable --workers "$workers"
python3 -m callguard.pipeline eval --audio --require-self-hostable --workers "$workers"
python3 -m callguard.pipeline freeze --audio --require-self-hostable

echo "Automated gate complete. Still required: human listening checklist, one genuinely new WAV through ingest/UI,"
echo "four-scene demo rehearsal, and hidden evaluation without changing prompt, policy, model or threshold."
