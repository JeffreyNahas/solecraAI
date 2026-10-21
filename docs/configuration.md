# Configuration

All settings come from environment variables (`.env`, loaded by `app/config.py`).
`.env` is git-ignored; `.env.example` lists every key. In Docker, compose builds
`DATABASE_URL` itself from the `POSTGRES_*` values.

| Variable | Default | Used for |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `solecra` / — / `solecra` | Postgres container; password read only on first DB creation |
| `DATABASE_URL` | `postgresql://solecra:solecra@localhost:5432/solecra` | App/Alembic outside Docker |
| `WHATSAPP_VERIFY_TOKEN` | — | Webhook verification (any random string, same as in Meta) |
| `WHATSAPP_APP_SECRET` | — | Signature check on every webhook POST |
| `WHATSAPP_ACCESS_TOKEN` | — | Sending messages (temporary token: ~24 h; system user token: long-lived) |
| `WHATSAPP_PHONE_NUMBER_ID` | — | Sending endpoint |
| `WHATSAPP_API_VERSION` | `v23.0` | Graph API version (match the app's, currently `v26.0`) |
| `WHATSAPP_DRY_RUN` | `false` | Log instead of send |
| `OWNER_PHONES` | — | Allowlist `phone:Name,phone:Name`, country code, digits (formatting stripped) |
| `WEBHOOK_CAPTURE_DIR` | unset | Dev: save raw webhook bodies to build fixtures |
| `OPENROUTER_API_KEY` | — | Phase 1 LLM calls |
| `RUN_FAILURE_ISSUE_THRESHOLD` | `3` | Failures in a row before a GitHub issue is opened |
| `GITHUB_ISSUES_TOKEN` | — | Fine-grained token, **Issues: read and write** on this repo only. Empty = reporter off |
| `GITHUB_REPO` | `JeffreyNahas/solecraAI` | Where `[agent]` issues are opened |
| `TIMEZONE` | `America/Montreal` | Times shown to owners and in issues |
| `BOT_DOMAIN` | — | Server: Caddy's HTTPS hostname |
| `LOG_LEVEL` | `INFO` | Logging |
| `WORKER_POLL_SECONDS` | `2.0` | Worker safety-net poll |
| `WORKER_MAX_ATTEMPTS` | `3` | Tries before a message is marked `failed` |
| `TEST_DATABASE_URL` | `postgresql://solecra:solecra@localhost:5432/solecra_test` | Tests only |
