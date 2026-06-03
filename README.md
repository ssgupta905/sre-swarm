# sre-swarm

Terminal-based agentic SRE swarm CLI for Kubernetes incident response.

See `sre-swarm-specs.md` for the full functional and technical specification.

## Install

```sh
pip install -e ".[dev]"
```

## Quick start

```sh
sre-swarm config init
sre-swarm monitor
```

## Commands

- `sre-swarm monitor` — launch the interactive TUI
- `sre-swarm investigate <id|description>` — run swarm non-interactively, prints JSON
- `sre-swarm replay <scenario>` — inject a chaos scenario and observe the swarm
- `sre-swarm status` — print current system status
- `sre-swarm incidents` — list local incidents
- `sre-swarm export <incident-id>` — export a Markdown report
- `sre-swarm config init` — interactive config wizard
