#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-pptx-transcribe}"
REQ_FILE="${REPO_ROOT}/tools/requirements-pptx-transcribe.txt"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "[setup] Repo root: ${REPO_ROOT}"
echo "[setup] Virtual environment: ${VENV_DIR}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[setup] Error: ${PYTHON_BIN} is not installed." >&2
  exit 1
fi

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "[setup] Error: requirements file missing at ${REQ_FILE}" >&2
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[setup] Creating virtual environment..."
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
  echo "[setup] Reusing existing virtual environment."
fi

echo "[setup] Installing dependencies from ${REQ_FILE}..."
"${VENV_DIR}/bin/python3" -m pip install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/python3" -m pip install -r "${REQ_FILE}"

echo "[setup] Running preflight checks..."
"${REPO_ROOT}/tools/preflight_pptx_transcribe.sh"

cat <<EOF
[setup] Completed.

To use this tooling in future sessions:
  export VENV_DIR="${VENV_DIR}"
  export TRANSCRIBE_CLI="\${TRANSCRIBE_CLI:-\${HOME}/.codex/skills/transcribe/scripts/transcribe_diarize.py}"

YouTube URL -> transcript:
  ${REPO_ROOT}/tools/youtube_to_transcript.sh "<youtube-url>"

OpenAI transcription fallback requires:
  export OPENAI_API_KEY="<your-key>"
EOF
