# Presentation Voice + Video Workflow

Date: 2026-03-04

This workflow creates a narrated MP4 from:

- A `.pptx` slide deck
- One audio file per slide (your own recorded voice)

The renderer enforces strict preflight checks to avoid silent failures.

## Important capability note

- This setup uses your **real recorded voice** files.
- It does **not** clone your voice from text.

## Tools

- `tools/export_slide_notes_for_recording.py`
- `tools/render_presentation_video.py`

## Step 1: Export speaker notes for recording

```bash
.venv-pptx-transcribe/bin/python3 tools/export_slide_notes_for_recording.py \
  --pptx "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-operations-cisco-template-5min.pptx" \
  --out-dir "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-ops-voice-script"
```

This writes:

- `slide01.txt` ... `slide05.txt`
- `index.txt` with per-slide word counts

## Step 2: Record your voice (one file per slide)

Create a folder, for example:

```text
/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-ops-voice-audio/
```

Add files named in slide order (natural sort):

```text
slide01.m4a
slide02.m4a
slide03.m4a
slide04.m4a
slide05.m4a
```

Supported formats: `.m4a`, `.mp3`, `.wav`, `.aiff`, `.aac`, `.m4b`

## Step 3: Preflight validation (recommended)

```bash
.venv-pptx-transcribe/bin/python3 tools/render_presentation_video.py \
  --pptx "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-operations-cisco-template-5min.pptx" \
  --audio-dir "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-ops-voice-audio" \
  --out "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-operations-cisco-template-5min-voice.mp4" \
  --preflight-only
```

## Step 4: Render final video

```bash
.venv-pptx-transcribe/bin/python3 tools/render_presentation_video.py \
  --pptx "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-operations-cisco-template-5min.pptx" \
  --audio-dir "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-ops-voice-audio" \
  --out "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-operations-cisco-template-5min-voice.mp4"
```

Shortcut for this deck:

```bash
tools/render_frontier_ops_cisco_with_voice.sh \
  "/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-ops-voice-audio"
```

Output metadata is written next to the MP4:

```text
frontier-operations-cisco-template-5min-voice.metadata.json
```

## Notes

- Slide images are exported via Keynote automation.
- ffmpeg is used for segment rendering and final concatenation.
- If slide count and audio file count do not match, rendering will fail fast.

## ElevenLabs Path (AI voice generation)

This path generates narration audio from slide notes using ElevenLabs, then renders the MP4.

Required environment variables:

```bash
export ELEVENLABS_API_KEY="<your-key>"
export ELEVENLABS_VOICE_ID="<your-voice-id>"
```

List available voices (to find your voice ID):

```bash
.venv-pptx-transcribe/bin/python3 tools/elevenlabs_list_voices.py
```

Full one-command pipeline for this deck:

```bash
tools/render_frontier_ops_cisco_with_elevenlabs.sh
```

With optional overrides:

```bash
tools/render_frontier_ops_cisco_with_elevenlabs.sh \
  --voice-id "<voice-id>" \
  --audio-speed 1.0 \
  --concat-mode reencode
```

Output:

```text
/Users/rpias/dev/vscode-dev-env/output/presentations/frontier-operations-cisco-template-5min-elevenlabs.mp4
```
