#!/bin/sh
# Make a fresh Debian host (Proxmox CT/VM, VPS) a Cairn node. Run as root on the host:
#   setup.sh "<ssh public key for user agent>"
# Same result as nodes/slim: user agent (key-only ssh), tmux, git, python3, Claude Code
# system-wide with the autoupdater off. Safe to re-run. Then add the node from the dashboard.
# sshd then accepts only agent with that key: root and every other user lose ssh access.
set -eu
KEY=${1:?usage: setup.sh "<ssh public key>"}
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin   # pct exec omits /usr/local

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  openssh-server tmux git python3 curl ca-certificates procps less

CLAUDE_VERSION=${CLAUDE_VERSION:-2.1.325}   # the same pin as nodes/slim/Dockerfile
if [ ! -x /usr/local/bin/claude ]; then
  HOME=/opt/claude-home CLAUDE_VERSION=$CLAUDE_VERSION bash -c 'curl -fsSL https://claude.ai/install.sh | bash -s -- "$CLAUDE_VERSION"'
  ln -sf "$(readlink -f /opt/claude-home/.local/bin/claude)" /usr/local/bin/claude
  chmod -R a+rX /opt/claude-home
fi
echo 'export DISABLE_AUTOUPDATER=1' > /etc/profile.d/cairn.sh
grep -qx 'DISABLE_AUTOUPDATER=1' /etc/environment || echo 'DISABLE_AUTOUPDATER=1' >> /etc/environment

printf '%s\n' "$KEY" | ssh-keygen -l -f - >/dev/null || { echo "setup.sh: not an ssh public key" >&2; exit 1; }

id agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent
passwd -l agent >/dev/null
chmod 700 /home/agent   # other local users must not read ~/.cairn, the repos or the transcripts
install -d -m 700 -o agent -g agent /home/agent/.ssh
printf '%s\n' "$KEY" > /home/agent/.ssh/authorized_keys
chown agent:agent /home/agent/.ssh/authorized_keys
chmod 600 /home/agent/.ssh/authorized_keys

# sshd keeps the first value it reads and reads the drop-ins in name order, so 00- beats
# cloud-init's 50-cloud-init.conf. The result is checked before sshd reloads.
CONF=/etc/ssh/sshd_config.d/00-cairn.conf
rm -f /etc/ssh/sshd_config.d/cairn.conf   # the name before 00-cairn.conf
cat > "$CONF" <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitUserEnvironment no
AllowUsers agent
EOF
mkdir -p /run/sshd
sshd -t || { rm -f "$CONF"; echo "setup.sh: sshd rejects the config; $CONF removed, sshd not reloaded" >&2; exit 1; }
EFFECTIVE=$(sshd -T)
for want in 'permitrootlogin no' 'passwordauthentication no' 'kbdinteractiveauthentication no' \
            'pubkeyauthentication yes' 'permituserenvironment no' 'allowusers agent'; do
  got=$(printf '%s\n' "$EFFECTIVE" | grep "^${want%% *} " || true)
  [ "$got" = "$want" ] || {
    echo "setup.sh: sshd -T gives '${got:-nothing}' where $CONF sets '$want'; another sshd config overrides or adds to it." >&2
    echo "setup.sh: fix that file, then run setup.sh again. sshd was not reloaded." >&2
    exit 1
  }
done
systemctl reload ssh 2>/dev/null || systemctl restart ssh

echo "tmux $(tmux -V | cut -d' ' -f2), git $(git --version | cut -d' ' -f3), python3 $(python3 -V | cut -d' ' -f2), claude $(DISABLE_AUTOUPDATER=1 claude --version | cut -d' ' -f1)"
