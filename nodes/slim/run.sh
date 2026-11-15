#!/usr/bin/env bash
# Run the Cairn slim sandbox node. See README.md.
set -euo pipefail

IMAGE=cairn-slim:0.1
PORT=2222
NAME=cairn-slim
PUBKEY=""
ACTION=run
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { echo "usage: run.sh [--port 2222] [--name cairn-slim] [--pubkey PATH] [--image cairn-slim:0.1] [--down | --purge]"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port)   PORT="${2:?--port needs a value}"; shift 2 ;;
    --name)   NAME="${2:?--name needs a value}"; shift 2 ;;
    --pubkey) PUBKEY="${2:?--pubkey needs a value}"; shift 2 ;;
    --image)  IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --down)   ACTION=down; shift ;;
    --purge)  ACTION=purge; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

HOME_VOL="${NAME}-home"
KEYS_VOL="${NAME}-hostkeys"

exists()  { docker container inspect "$NAME" >/dev/null 2>&1; }
running() { [ "$(docker container inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = true ]; }

if [ "$ACTION" != run ]; then
  exists && docker rm -f "$NAME" >/dev/null && echo "removed container $NAME" || echo "no container $NAME"
  if [ "$ACTION" = purge ]; then
    printf 'Delete volumes %s and %s (all agent work and the host key)? [y/N] ' "$HOME_VOL" "$KEYS_VOL"
    read -r ans || ans=n
    case "$ans" in
      y|Y) docker volume rm "$HOME_VOL" "$KEYS_VOL" 2>/dev/null || true; echo "volumes removed" ;;
      *) echo "volumes kept" ;;
    esac
  else
    echo "volumes kept: $HOME_VOL $KEYS_VOL"
  fi
  exit 0
fi

if [ -z "$PUBKEY" ]; then
  for k in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub"; do
    [ -f "$k" ] && { PUBKEY="$k"; break; }
  done
fi
[ -n "$PUBKEY" ] && [ -f "$PUBKEY" ] || { echo "no public key found; pass --pubkey PATH" >&2; exit 1; }
PUBKEY="$(cd "$(dirname "$PUBKEY")" && pwd)/$(basename "$PUBKEY")"

if running; then
  echo "$NAME already running"
elif exists; then
  docker start "$NAME" >/dev/null
  echo "started $NAME"
else
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    [ "$IMAGE" = cairn-slim:0.1 ] || { echo "image $IMAGE not found; build it first" >&2; exit 1; }
    docker build -t "$IMAGE" "$HERE"
  fi
  # sshd needs most of Docker's default capabilities (setuid, chroot, chown, audit), so only these go.
  # hub/cairn_hub/sandbox.py (HARDEN) uses the same flags; a hub test compares them.
  # The dev image's passwordless sudo is setuid, which no-new-privileges would block.
  HARDEN=(--cap-drop NET_RAW --cap-drop MKNOD --pids-limit 4096)
  case "$IMAGE" in
    cairn-dev:*) ;;
    *) HARDEN+=(--security-opt no-new-privileges) ;;
  esac
  docker run -d --name "$NAME" --restart unless-stopped "${HARDEN[@]}" \
    -p "127.0.0.1:${PORT}:22" \
    -v "${HOME_VOL}:/home/agent" \
    -v "${KEYS_VOL}:/etc/ssh/hostkeys" \
    -v "${PUBKEY}:/etc/cairn/authorized_key:ro" \
    "$IMAGE" >/dev/null
  echo "created $NAME"
fi

PORT_NOW="$(docker port "$NAME" 22/tcp 2>/dev/null | head -n1 | sed 's/.*://')"
PORT_NOW="${PORT_NOW:-$PORT}"
cat <<MSG

ssh -p $PORT_NOW agent@127.0.0.1

Suggested ~/.ssh/config entry (not written for you):

Host $NAME
    HostName 127.0.0.1
    Port $PORT_NOW
    User agent
    IdentityFile ${PUBKEY%.pub}
    UserKnownHostsFile ~/.cairn/known_hosts
    StrictHostKeyChecking accept-new
MSG
