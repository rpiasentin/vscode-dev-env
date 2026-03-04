#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv-pptx-transcribe}"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
TRANSCRIBE_CLI="${TRANSCRIBE_CLI:-${CODEX_HOME}/skills/transcribe/scripts/transcribe_diarize.py}"
REQUIRE_OPENAI_KEY=0

usage() {
  cat <<'USAGE'
Usage: tools/preflight_pptx_transcribe.sh [--require-openai-key]

Checks all local prerequisites for:
- PowerPoint generation via python-pptx
- YouTube transcript retrieval and OpenAI transcription fallback
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --require-openai-key)
      REQUIRE_OPENAI_KEY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[preflight] Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

failures=()

record_fail() {
  failures+=("$1")
  echo "[preflight] FAIL: $1" >&2
}

record_pass() {
  echo "[preflight] PASS: $1"
}

if command -v python3 >/dev/null 2>&1; then
  record_pass "python3 is available"
else
  record_fail "python3 is missing"
fi

if [[ -x "${VENV_DIR}/bin/python3" ]]; then
  record_pass "virtual environment exists at ${VENV_DIR}"
else
  record_fail "virtual environment missing at ${VENV_DIR} (run tools/setup_pptx_transcribe.sh)"
fi

if [[ -f "${TRANSCRIBE_CLI}" ]]; then
  record_pass "transcribe CLI found at ${TRANSCRIBE_CLI}"
else
  record_fail "transcribe CLI missing at ${TRANSCRIBE_CLI}"
fi

if [[ -x "${VENV_DIR}/bin/python3" ]]; then
  for module in openai pptx yt_dlp youtube_transcript_api; do
    if "${VENV_DIR}/bin/python3" -c "import ${module}" >/dev/null 2>&1; then
      record_pass "python module '${module}' import succeeded"
    else
      record_fail "python module '${module}' missing in ${VENV_DIR}"
    fi
  done

  if "${VENV_DIR}/bin/python3" -m yt_dlp --version >/dev/null 2>&1; then
    record_pass "yt-dlp runtime check succeeded"
  else
    record_fail "yt-dlp runtime check failed"
  fi
fi

mkdir -p "${REPO_ROOT}/output/transcribe/youtube"
probe_file="${REPO_ROOT}/output/transcribe/youtube/.preflight_write_test"
if touch "${probe_file}" >/dev/null 2>&1; then
  rm -f "${probe_file}"
  record_pass "workspace output directory is writable"
else
  record_fail "workspace output directory is not writable"
fi

if [[ ${REQUIRE_OPENAI_KEY} -eq 1 ]]; then
  if [[ -n "${OPENAI_API_KEY:-}" ]]; then
    record_pass "OPENAI_API_KEY is set"
  else
    record_fail "OPENAI_API_KEY is not set"
  fi
fi

if [[ ${#failures[@]} -gt 0 ]]; then
  echo "[preflight] Summary: ${#failures[@]} check(s) failed." >&2
  for item in "${failures[@]}"; do
    echo "  - ${item}" >&2
  done
  exit 1
fi

echo "[preflight] Summary: all checks passed."
