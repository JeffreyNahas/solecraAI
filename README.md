# solecraAI

AI operations assistant for Solecra (blinds & curtains, Montréal), used by the owners over WhatsApp.

- **What and why:** [`CLAUDE.md`](CLAUDE.md) (design, rules, build plan, decisions)
- **How to run, test, deploy:** [`RUNBOOK.md`](RUNBOOK.md)

Status: Gate 1 (WhatsApp approval loop that survives restarts) built and awaiting review.

```bash
cp .env.example .env && docker compose up --build
```
