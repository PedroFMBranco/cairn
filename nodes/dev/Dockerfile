# A slim sandbox for leads that build real apps: passwordless sudo, so the
# lead installs what it needs, plus the usual toolchain preinstalled.
FROM cairn-slim:0.1

# Node.js from NodeSource's apt repo, pinned to a version; its signing key must have this fingerprint
# (the key served at deb.nodesource.com on 2026-11-14), so no setup script runs.
ARG NODE_VERSION=22.23.5-1nodesource1
ARG NODESOURCE_FPR=6F71F525282841EEDAF851B42F59B5F99B1BE0B4

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      sudo build-essential pkg-config unzip zip jq ripgrep rsync wget gnupg \
      python3-venv python3-pip python3-dev \
      postgresql postgresql-contrib postgis postgresql-15-postgis-3 \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key -o /tmp/nodesource.key \
 && export GNUPGHOME="$(mktemp -d)" \
 && [ "$(gpg --show-keys --with-colons /tmp/nodesource.key | awk -F: '$1 == "fpr" {print $10; exit}')" = "$NODESOURCE_FPR" ] \
 && gpg --batch --dearmor -o /usr/share/keyrings/nodesource.gpg /tmp/nodesource.key \
 && rm -rf /tmp/nodesource.key "$GNUPGHOME" \
 && printf '%s\n' 'Types: deb' 'URIs: https://deb.nodesource.com/node_22.x' 'Suites: nodistro' 'Components: main' \
      "Architectures: $(dpkg --print-architecture)" 'Signed-By: /usr/share/keyrings/nodesource.gpg' \
      > /etc/apt/sources.list.d/nodesource.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends "nodejs=$NODE_VERSION" \
 && corepack enable \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
 && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
 && chmod 0440 /etc/sudoers.d/agent

# Newer Claude Code than an older cairn-slim:0.1 may hold (the fallbackModel setting).
ARG CLAUDE_VERSION=2.1.325
RUN rm -rf /opt/claude-home \
 && HOME=/opt/claude-home bash -c 'curl -fsSL https://claude.ai/install.sh | bash -s -- "$CLAUDE_VERSION"' \
 && ln -sf "$(readlink -f /opt/claude-home/.local/bin/claude)" /usr/local/bin/claude \
 && chmod -R a+rX /opt/claude-home \
 && claude --version | grep -q "^$CLAUDE_VERSION "
