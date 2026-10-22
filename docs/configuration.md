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
| `OPENROUTER_API_KEY` | — | All LLM calls (`app/llm.py`). Empty while leads wait = scoring/research runs fail and alert |
| `LLM_MODEL_SCORING` | `anthropic/claude-haiku-4.5` | Model for lead scoring (cheap, high volume) |
| `LLM_MODEL_RESEARCH` | `anthropic/claude-haiku-4.5` | Model for the lead research loop |
| `LLM_FALLBACK_MODELS` | — | Comma-separated OpenRouter model ids tried if the main one fails |
| `PERMITS_LOOKBACK_DAYS` | `30` | First permits pull goes back this far |
| `PERMITS_MAX_KM` | `30` | Max distance from downtown Montréal |
| `PERMITS_MIN_UNITS` | `4` | Min units for multi-residential permits |
| `LEAD_SCORE_THRESHOLD` | `60` | Below this score a lead is disqualified |
| `SCORING_MAX_PER_RUN` | `100` | Max leads scored per run (cost cap) |
| `RESEARCH_MAX_PER_DAY` | `15` | Leads researched per Montréal day (≈ $0.08 each, live 2026-10-22) |
| `RESEARCH_MAX_TURNS` | `8` | Model turns per lead in the research loop |
| `RESEARCH_MAX_SEARCHES` | `4` | Web searches per lead (Exa via OpenRouter, ≈ $0.007 each) |
| `RESEARCH_MAX_USD_PER_DAY` | `1.5` | Safety stop: research model + search spend per Montréal day (USD), checked before each lead |
| `RUN_FAILURE_ISSUE_THRESHOLD` | `3` | Failures in a row before a GitHub issue is opened |
| `GITHUB_ISSUES_TOKEN` | — | Fine-grained token, **Issues: read and write** on this repo only. Empty = reporter off |
| `GITHUB_REPO` | `JeffreyNahas/solecraAI` | Where `[agent]` issues are opened |
| `TIMEZONE` | `America/Montreal` | Times shown to owners and in issues |
| `BOT_DOMAIN` | — | Server: Caddy's HTTPS hostname |
| `LOG_LEVEL` | `INFO` | Logging |
| `WORKER_POLL_SECONDS` | `2.0` | Worker safety-net poll |
| `WORKER_MAX_ATTEMPTS` | `3` | Tries before a message is marked `failed` |
| `TEST_DATABASE_URL` | `postgresql://solecra:solecra@localhost:5432/solecra_test` | Tests only |
