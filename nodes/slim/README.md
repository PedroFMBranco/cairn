# Cairn slim sandbox node

Debian bookworm-slim with openssh-server, tmux, git, python3, curl, procps, less and Claude Code. About 770 MB on disk (211 MB compressed). Claude Code is installed under `/opt/claude-home` and symlinked at `/usr/local/bin/claude`, so a volume on `/home/agent` does not hide it. `DISABLE_AUTOUPDATER=1` is set. The base image is pinned by digest and Claude Code by version (`--build-arg CLAUDE_VERSION=<version>` picks another); the install script itself is fetched at build time and checks the binary's sha256 against the release manifest.

## Use

```
./run.sh [--port 2222] [--name cairn-slim] [--pubkey PATH]
./run.sh --down     # stop and remove the container, keep volumes
./run.sh --purge    # also delete the volumes (asks y/N)
```

`run.sh` builds `cairn-slim:0.1` if missing, then creates, starts or no-ops depending on state. It prints the ssh command and a `~/.ssh/config` snippet; it never writes the config.

```
ssh -p 2222 agent@127.0.0.1
```

## Layout

| what | where |
|---|---|
| user | `agent`, uid 1000, no password, no sudo |
| home | named volume `<name>-home` on `/home/agent`, mode 0700 (set again at every start) |
| sshd host keys | named volume `<name>-hostkeys` on `/etc/ssh/hostkeys`; created on first start, so the fingerprint survives recreation |
| authorized key | your `.pub` mounted read-only at `/etc/cairn/authorized_key` (default: first of `~/.ssh/id_ed25519.pub`, `~/.ssh/id_rsa.pub`) |
| port | published on `127.0.0.1` only |
| limits | `--cap-drop NET_RAW --cap-drop MKNOD`, `--pids-limit 4096`, and `no-new-privileges` for every image except `cairn-dev:*`, whose sudo needs setuid |

sshd: key-only, no root login, only `agent`.

## Notes

- Changing `--pubkey` on an existing container needs `--down` then `run.sh` (mounts and limits are fixed at creation).
- `run.sh` builds the image only when `cairn-slim:0.1` is missing. After a change to the Dockerfile, `docker rmi cairn-slim:0.1` first.
- No token or `.env` is baked in. At each start the hub sends the Claude login on the ssh command's stdin, and `cairn-node` hands it to the lead's shell through a FIFO, never through the tmux environment (see "How login tokens are kept" in `docs/how-it-works.md`).
- After `--purge` the host key changes; clear the old entry from `~/.cairn/known_hosts`.
