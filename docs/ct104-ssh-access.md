# CT104 Root SSH Access (Codex)

Date: 2026-02-27
Target: `root@192.168.2.105`

## Key identity

- Type: `ed25519`
- Comment: `codex-ct104-root-2026-02-27`
- Fingerprint: `SHA256:FXNKt4nBCuYNEMdjGOhfkhER9SnRJjJ+F+WLxu+VSQg`

## Public key to authorize on CT104

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDYm3MhtJGTxidv1awD4NlWQ97FTi6uHhrE5ZUVRxcXE codex-ct104-root-2026-02-27
```

## Install on CT104 (run as root)

```bash
set -euo pipefail
install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
grep -qxF 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDYm3MhtJGTxidv1awD4NlWQ97FTi6uHhrE5ZUVRxcXE codex-ct104-root-2026-02-27' /root/.ssh/authorized_keys || \
  echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDYm3MhtJGTxidv1awD4NlWQ97FTi6uHhrE5ZUVRxcXE codex-ct104-root-2026-02-27' >> /root/.ssh/authorized_keys

sshd -t
systemctl reload ssh || systemctl reload sshd || true
```

## Verify from local machine

The private key is stored locally (not committed):

```text
/Users/rpias/dev/vscode-dev-env/.notes_access/ssh/ct104_root_ed25519
```

Test login:

```bash
ssh -i /Users/rpias/dev/vscode-dev-env/.notes_access/ssh/ct104_root_ed25519 root@192.168.2.105
```
