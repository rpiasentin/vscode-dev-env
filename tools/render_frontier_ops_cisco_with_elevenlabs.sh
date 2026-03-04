#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv-pptx-transcribe/bin/python3}"

PPTX_PATH="${REPO_ROOT}/output/presentations/frontier-operations-cisco-template-5min.pptx"
SCRIPT_OUT_DIR="${REPO_ROOT}/output/presentations/frontier-ops-elevenlabs-script"
AUDIO_OUT_DIR="${REPO_ROOT}/output/presentations/frontier-ops-elevenlabs-audio"
VIDEO_OUT_PATH="${REPO_ROOT}/output/presentations/frontier-operations-cisco-template-5min-elevenlabs.mp4"
SCRIPT_MD_PATH=""

VOICE_ID="${ELEVENLABS_VOICE_ID:-}"
AUDIO_SPEED="1.0"
CONCAT_MODE="reencode"
PREVIEW_ONLY="0"
SKIP_EXISTING="0"

usage() {
  cat <<EOF
Usage:
  tools/render_frontier_ops_cisco_with_elevenlabs.sh [options]

Options:
  --pptx <path>          Source presentation path (.pptx)
  --script-out-dir <dir> Output directory for slideXX.txt scripts
  --audio-out-dir <dir>  Output directory for generated slide audio
  --video-out <path>     Output narrated MP4 path
  --script-md <path>     Markdown script source (uses Slide 1..N headings)
  --voice-id <id>        ElevenLabs voice id (overrides ELEVENLABS_VOICE_ID)
  --audio-speed <value>  Narration speed multiplier in render step (default: 1.0)
  --concat-mode <mode>   auto|copy|reencode (default: reencode)
  --skip-existing        Keep existing generated slide audio files
  --preview-only         Export scripts + run preflight only (no TTS API calls, no video render)
  -h, --help             Show this help

Required env:
  ELEVENLABS_API_KEY must be set for generation (unless --preview-only)
  ELEVENLABS_VOICE_ID must be set or provided via --voice-id
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pptx)
      PPTX_PATH="$2"
      shift 2
      ;;
    --script-out-dir)
      SCRIPT_OUT_DIR="$2"
      shift 2
      ;;
    --audio-out-dir)
      AUDIO_OUT_DIR="$2"
      shift 2
      ;;
    --video-out)
      VIDEO_OUT_PATH="$2"
      shift 2
      ;;
    --script-md)
      SCRIPT_MD_PATH="$2"
      shift 2
      ;;
    --voice-id)
      VOICE_ID="$2"
      shift 2
      ;;
    --audio-speed)
      AUDIO_SPEED="$2"
      shift 2
      ;;
    --concat-mode)
      CONCAT_MODE="$2"
      shift 2
      ;;
    --skip-existing)
      SKIP_EXISTING="1"
      shift
      ;;
    --preview-only)
      PREVIEW_ONLY="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Error: Python runtime not found at ${PYTHON_BIN}" >&2
  exit 1
fi

PPTX_PATH="$(cd "$(dirname "${PPTX_PATH}")" && pwd)/$(basename "${PPTX_PATH}")"
SCRIPT_OUT_DIR="$(mkdir -p "${SCRIPT_OUT_DIR}" && cd "${SCRIPT_OUT_DIR}" && pwd)"
AUDIO_OUT_DIR="$(mkdir -p "${AUDIO_OUT_DIR}" && cd "${AUDIO_OUT_DIR}" && pwd)"
VIDEO_OUT_PATH="$(cd "$(dirname "${VIDEO_OUT_PATH}")" && pwd)/$(basename "${VIDEO_OUT_PATH}")"

if [[ ! -f "${PPTX_PATH}" ]]; then
  echo "Error: PPTX not found: ${PPTX_PATH}" >&2
  exit 1
fi

if [[ -n "${SCRIPT_MD_PATH}" ]]; then
  SCRIPT_MD_PATH="$(cd "$(dirname "${SCRIPT_MD_PATH}")" && pwd)/$(basename "${SCRIPT_MD_PATH}")"
  if [[ ! -f "${SCRIPT_MD_PATH}" ]]; then
    echo "Error: markdown script not found: ${SCRIPT_MD_PATH}" >&2
    exit 1
  fi
fi

if [[ "${PREVIEW_ONLY}" != "1" ]]; then
  if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
    echo "Error: ELEVENLABS_API_KEY is not set" >&2
    exit 1
  fi
  if [[ -z "${VOICE_ID}" ]]; then
    echo "Error: ELEVENLABS_VOICE_ID is not set (or pass --voice-id)" >&2
    exit 1
  fi
fi

mkdir -p "${SCRIPT_OUT_DIR}" "${AUDIO_OUT_DIR}"

if [[ -n "${SCRIPT_MD_PATH}" ]]; then
  SLIDE_COUNT="$("${PYTHON_BIN}" - <<PY
import re, zipfile
p="${PPTX_PATH}"
n=0
with zipfile.ZipFile(p, "r") as z:
    for name in z.namelist():
        if re.match(r"ppt/slides/slide\\d+\\.xml$", name):
            n += 1
print(n)
PY
)"
  echo "[pipeline] Building slide scripts from markdown: ${SCRIPT_MD_PATH}"
  "${PYTHON_BIN}" "${REPO_ROOT}/tools/prepare_slide_scripts_from_markdown.py" \
    --md "${SCRIPT_MD_PATH}" \
    --out-dir "${SCRIPT_OUT_DIR}" \
    --expected-slides "${SLIDE_COUNT}"
else
  echo "[pipeline] Exporting slide notes..."
  "${PYTHON_BIN}" "${REPO_ROOT}/tools/export_slide_notes_for_recording.py" \
    --pptx "${PPTX_PATH}" \
    --out-dir "${SCRIPT_OUT_DIR}"
fi

if [[ "${PREVIEW_ONLY}" == "1" ]]; then
  echo "[pipeline] Preview mode: skipping ElevenLabs generation and video render."
  echo "[pipeline] Notes exported to: ${SCRIPT_OUT_DIR}"
  echo "[pipeline] To list voices: ${PYTHON_BIN} ${REPO_ROOT}/tools/elevenlabs_list_voices.py"
  echo "[pipeline] To run full pipeline, remove --preview-only and set ELEVENLABS_API_KEY + voice id."
  exit 0
else
  echo "[pipeline] Generating audio with ElevenLabs..."
  TTS_ARGS=(
    --script-dir "${SCRIPT_OUT_DIR}"
    --out-dir "${AUDIO_OUT_DIR}"
    --voice-id "${VOICE_ID}"
    --use-speaker-boost
  )
  if [[ "${SKIP_EXISTING}" == "1" ]]; then
    TTS_ARGS+=(--skip-existing)
  fi
  "${PYTHON_BIN}" "${REPO_ROOT}/tools/elevenlabs_generate_slide_audio.py" "${TTS_ARGS[@]}"
fi

echo "[pipeline] Rendering narrated video..."
RENDER_ARGS=(
  --pptx "${PPTX_PATH}"
  --audio-dir "${AUDIO_OUT_DIR}"
  --out "${VIDEO_OUT_PATH}"
  --audio-speed "${AUDIO_SPEED}"
  --concat-mode "${CONCAT_MODE}"
  --high-priority
)
"${PYTHON_BIN}" "${REPO_ROOT}/tools/render_presentation_video.py" "${RENDER_ARGS[@]}"
echo "[pipeline] Done: ${VIDEO_OUT_PATH}"
