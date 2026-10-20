# Technical documentation

How the system works inside. For *running* it (setup, deploy, troubleshooting) see
[`../RUNBOOK.md`](../RUNBOOK.md); for goals, rules and the build plan see [`../CLAUDE.md`](../CLAUDE.md).

| Doc | Covers |
|---|---|
| [architecture.md](architecture.md) | Containers, how a request travels from WhatsApp to the code, local vs server |
| [data-model.md](data-model.md) | Every table, its columns and statuses, idempotency keys |
| [workflows.md](workflows.md) | Background worker, message handling, approval lifecycle, LangGraph graphs |
| [whatsapp-integration.md](whatsapp-integration.md) | Webhook verify/signature, payloads, sending, buttons, 24 h window |
| [configuration.md](configuration.md) | Every environment variable |
| [security-and-compliance.md](security-and-compliance.md) | Each non-negotiable rule and where the code enforces it |

## Keeping these up to date

Docs are updated **in the same PR** as the code. Which doc goes with which code is defined once,
in [`doc-map.toml`](doc-map.toml); CI (`scripts/check_docs.py`) fails a PR that changes mapped
code without touching the matching doc. In Claude Code, run `/update-docs` to update them.

If a change truly needs no doc update (e.g. a refactor with identical behaviour), put
`docs: none` on its own line in a commit message of the PR.
