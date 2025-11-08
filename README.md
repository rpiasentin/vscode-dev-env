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

