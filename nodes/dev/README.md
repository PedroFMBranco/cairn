# Cairn dev sandbox node

The slim sandbox plus what a lead needs to build a real app: passwordless `sudo` for `agent`, build-essential, Node 22 with corepack, Python venv/pip, PostgreSQL 15 with PostGIS 3, jq, ripgrep, and a newer Claude Code. Nothing starts at boot except sshd; the lead runs `sudo service postgresql start` itself.

```
docker build -t cairn-dev:0.1 nodes/dev          # needs cairn-slim:0.1 (built by nodes/slim/run.sh)
nodes/slim/run.sh --image cairn-dev:0.1 --name cairn-strand --port 2222
```

`--down` and `--purge` work as for the slim node. `sudo` makes the container root-equivalent for the lead: keep it disposable, as with any node. `run.sh` starts `cairn-dev:*` images without `no-new-privileges`, because that option would block sudo.

Node.js comes from NodeSource's apt repo at a pinned version (`NODE_VERSION`), and the build refuses a signing key whose fingerprint differs from `NODESOURCE_FPR`. Claude Code is pinned by `CLAUDE_VERSION`, as in the slim image.
