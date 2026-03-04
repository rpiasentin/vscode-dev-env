#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv-pptx-transcribe/bin/python3}"

PPTX_PATH="${REPO_ROOT}/output/presentations/frontier-operations-cisco-template-5min.pptx"
OUT_PATH="${REPO_ROOT}/output/presentations/frontier-operations-cisco-template-5min-voice.mp4"

usage() {
  cat <<EOF
Usage:
  tools/render_frontier_ops_cisco_with_voice.sh <audio-dir> [--preflight-only]

Arguments:
  <audio-dir>       Directory containing slide01..slide05 audio files in your voice

Optional flags:
  --preflight-only  Run validation checks only

Example:
  tools/render_frontier_ops_cisco_with_voice.sh \\
    "${REPO_ROOT}/output/presentations/frontier-ops-voice-audio"
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

AUDIO_DIR="$1"
shift || true

EXTRA_ARGS=()
if [[ $# -gt 0 ]]; then
  EXTRA_ARGS=("$@")
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Error: Python runtime not found at ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  exec "${PYTHON_BIN}" "${REPO_ROOT}/tools/render_presentation_video.py" \
    --pptx "${PPTX_PATH}" \
    --audio-dir "${AUDIO_DIR}" \
    --out "${OUT_PATH}" \
    "${EXTRA_ARGS[@]}"
else
  exec "${PYTHON_BIN}" "${REPO_ROOT}/tools/render_presentation_video.py" \
    --pptx "${PPTX_PATH}" \
    --audio-dir "${AUDIO_DIR}" \
    --out "${OUT_PATH}"
fi
