# Security

## Reporting a problem

Report it privately: on this repository's **Security** tab, choose **Report a vulnerability**. Please do not open a public issue for it.

Say which version you run (`hub/cairn --version` and the `cairn-node` version on the Nodes page) and how to reproduce it.

## Supported versions

Only the latest tagged release gets fixes.

## What is by design

Leads run `claude --dangerously-skip-permissions` with nobody watching, so a lead can do anything the node's user can do on that node. That is the design, not a vulnerability: nodes must be disposable machines, as the README says. What the hub does to keep a node away from the Mac, your tokens and other nodes is described in the security model in [docs/how-it-works.md](docs/how-it-works.md); a way around any of that is worth reporting.
