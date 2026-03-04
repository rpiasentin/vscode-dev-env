#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-pptx-transcribe}"

if [[ ! -x "${VENV_DIR}/bin/python3" ]]; then
  echo "Error: ${VENV_DIR}/bin/python3 is missing. Run tools/setup_pptx_transcribe.sh first." >&2
  exit 1
fi

"${REPO_ROOT}/tools/preflight_pptx_transcribe.sh"

exec "${VENV_DIR}/bin/python3" "${REPO_ROOT}/tools/youtube_transcript.py" "$@"
