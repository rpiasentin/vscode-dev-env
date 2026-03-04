# PowerPoint + YouTube Transcript Tooling

Date: 2026-03-04

This workspace now includes durable setup scripts for:

- PowerPoint deck generation support (`python-pptx`)
- YouTube URL transcript retrieval
  - Captions-first path (no OpenAI key required if captions exist)
  - OpenAI transcription fallback from downloaded audio

## One-time setup

```bash
./tools/setup_pptx_transcribe.sh
```

## Preflight checks

```bash
./tools/preflight_pptx_transcribe.sh
```

Require API key check:

```bash
./tools/preflight_pptx_transcribe.sh --require-openai-key
```

## Create transcript from a YouTube URL

```bash
./tools/youtube_to_transcript.sh "https://www.youtube.com/watch?v=<video-id>"
```

Outputs are written to:

```text
output/transcribe/youtube/
```

## Notes

- OpenAI fallback requires `OPENAI_API_KEY` in your shell.
- Transcription uses the installed Codex transcribe skill script by default:
  `~/.codex/skills/transcribe/scripts/transcribe_diarize.py`
- Some YouTube videos block direct audio download (HTTP 403) without additional
  browser cookie context. In those cases, captions mode still works when captions
  exist, and is the recommended default path.
- The setup is workspace-local and reproducible from:
  - `tools/requirements-pptx-transcribe.txt`
  - `tools/setup_pptx_transcribe.sh`
  - `tools/preflight_pptx_transcribe.sh`
  - `tools/youtube_transcript.py`
