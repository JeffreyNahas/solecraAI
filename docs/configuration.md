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
| `LLM_MODEL_DRAFTING` | `anthropic/claude-sonnet-5.5` | Model for outreach email drafts and AI edits (client-facing: Sonnet-class) |
| `LLM_FALLBACK_MODELS` | — | Comma-separated OpenRouter model ids tried if the main one fails |
| `PERMITS_LOOKBACK_DAYS` | `30` | First permits pull goes back this far |
| `PERMITS_MAX_KM` | `30` | Max distance from downtown Montréal |
| `PERMITS_MIN_UNITS` | `4` | Min units for multi-residential permits |
| `LEAD_SCORE_THRESHOLD` | `60` | Below this score a lead is disqualified |
| `SCORING_MAX_PER_RUN` | `100` | Max leads scored per run (cost cap) |
| `RESEARCH_MAX_PER_DAY` | `15` | Leads researched per Montréal day (≈ $0.08 each, live 2026-10-22) |
| `RESEARCH_MAX_TURNS` | `8` | Model turns per lead in the research loop |
| `RESEARCH_MAX_SEARCHES` | `4` | Web searches per lead (Exa via OpenRouter, ≈ $0.007 each) |
| `OUTREACH_DAILY_CAP` | `15` | New outreach emails per day (default; owners can change it at runtime: `settings.outreach_daily_cap`) |
| `OUTREACH_DRAFT_EXPIRY_BUSINESS_DAYS` | `2` | Undecided drafts expire after this many Québec business days |
| `OUTREACH_CONSENT_MAX_AGE_DAYS` | `30` | Consent evidence older than this is re-checked before drafting |
| `OUTREACH_ALLOWED_NUMBERS` | — | Numbers emails may contain besides the lead's own facts (owners' approved list), comma-separated |
| `OUTREACH_BLOCKED_TERMS` | — | Supplier / brand names emails must never contain, comma-separated |
| `COMPANY_NAME` | `Solecra` | Company in the signature and CASL footer |
| `COMPANY_SENDER_NAME` | — | Signs every email (decided: Carl Abi Yaghi) |
| `COMPANY_ADDRESS`, `COMPANY_PHONE`, `COMPANY_EMAIL`, `COMPANY_WEBSITE` | — | CASL identification in every email; drafting refuses to run if any is empty. `COMPANY_WEBSITE` is the only link host allowed in emails |
| `PUBLIC_BASE_URL` | — | https base of unsubscribe links (`<url>/u/<token>`): `https://bot.<domain>` once deployed |
| `EMAIL_MODE` | `test` | `test`: only `EMAIL_TEST_RECIPIENTS` can ever receive outreach email, others are held. `live` also needs the step-15 sign-off row, a permanent https `PUBLIC_BASE_URL` and the full identity (`app/outreach/golive.py`) |
| `EMAIL_TEST_RECIPIENTS` | — | Comma-separated test inboxes |
| `EMAIL_SEND_START_HOUR`, `EMAIL_SEND_END_HOUR` | `9`, `16` | Sending window, Montréal time (the schedule also only fires 09:00–15:55) |
| `EMAIL_GAP_MIN_MINUTES`, `EMAIL_GAP_MAX_MINUTES` | `15`, `35` | Random wait between two emails |
| `EMAIL_TEST_IGNORES_SCHEDULE` | `false` | Test mode only: send as soon as the job runs, any hour, no gap |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, `GMAIL_SENDER` | — | Gmail sending. Put them in **`.env.gmail`** (git-ignored; read by the scheduler container only). Created by `python -m scripts.gmail_auth` |
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
