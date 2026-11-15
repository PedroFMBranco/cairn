# bookworm-slim as of 2026-11-14 (multi-arch index digest)
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openssh-server tmux git python3 curl ca-certificates procps less \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Claude Code, system-wide: install under /opt with a throwaway HOME, then
# symlink, so a volume on /home/agent cannot hide it. The installer checks the binary's sha256
# against the release manifest; the version is pinned here.
ARG CLAUDE_VERSION=2.1.325
RUN HOME=/opt/claude-home bash -c 'curl -fsSL https://claude.ai/install.sh | bash -s -- "$CLAUDE_VERSION"' \
 && ln -sf "$(readlink -f /opt/claude-home/.local/bin/claude)" /usr/local/bin/claude \
 && chmod -R a+rX /opt/claude-home \
 && claude --version | grep -q "^$CLAUDE_VERSION "
ENV DISABLE_AUTOUPDATER=1

RUN useradd -m -u 1000 -s /bin/bash agent \
 && passwd -l agent \
 && chmod 700 /home/agent \
 && mkdir -p /run/sshd /etc/ssh/hostkeys /etc/cairn \
 && printf '%s\n' \
      'Port 22' \
      'HostKey /etc/ssh/hostkeys/ssh_host_ed25519_key' \
      'HostKey /etc/ssh/hostkeys/ssh_host_rsa_key' \
      'PermitRootLogin no' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' \
      'PubkeyAuthentication yes' \
      'AllowUsers agent' \
      'AuthorizedKeysFile /etc/cairn/authorized_key' \
      'StrictModes no' \
      'PermitUserEnvironment no' \
      'AcceptEnv LANG LC_*' \
      > /etc/ssh/sshd_config.d/cairn.conf \
 # ssh sessions do not inherit container ENV; make the env visible to them
 && echo 'export DISABLE_AUTOUPDATER=1' > /etc/profile.d/cairn.sh \
 && echo 'DISABLE_AUTOUPDATER=1' >> /etc/environment

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
