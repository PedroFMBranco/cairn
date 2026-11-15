#!/bin/sh
set -e
# Host keys live on a volume at /etc/ssh/hostkeys so they survive recreation.
[ -f /etc/ssh/hostkeys/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/hostkeys/ssh_host_ed25519_key
[ -f /etc/ssh/hostkeys/ssh_host_rsa_key ] || ssh-keygen -q -t rsa -b 3072 -N '' -f /etc/ssh/hostkeys/ssh_host_rsa_key
mkdir -p /run/sshd
# /home/agent is a volume that may predate the image's chmod.
chmod 700 /home/agent || echo "entrypoint: could not chmod /home/agent (needs CAP_FOWNER); left as it is" >&2
exec /usr/sbin/sshd -D -e
