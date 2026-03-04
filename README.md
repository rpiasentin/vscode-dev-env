# VS Code Dev Environment

This repo contains my global VS Code configuration so I can recreate my setup on any machine.

## Files

- settings.json      - Global VS Code settings (User settings)
- keybindings.json   - Global keybindings (optional)
- extensions.txt     - List of VS Code extensions

## Recreate on a new machine

1. Install VS Code and the code command.

2. Install extensions:

   xargs -n 1 code --install-extension < extensions.txt

3. Copy settings/keybindings (macOS):

   - Put settings.json into:
     ~/Library/Application Support/Code/User/settings.json
   - Put keybindings.json into:
     ~/Library/Application Support/Code/User/keybindings.json (optional)

4. Open VS Code and turn on Settings Sync (sign in with GitHub).

## Workspace Tooling: PowerPoint + YouTube Transcript

This workspace also includes local automation for:

- PowerPoint prep dependencies (`python-pptx`)
- YouTube URL transcript retrieval (captions-first, with OpenAI fallback)

Setup:

```bash
./tools/setup_pptx_transcribe.sh
```

Run transcript workflow:

```bash
./tools/youtube_to_transcript.sh "https://www.youtube.com/watch?v=<video-id>"
```

Details: `docs/pptx-youtube-transcript-setup.md`
