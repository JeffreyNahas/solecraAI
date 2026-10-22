# Architecture

## Containers

Defined in `docker-compose.yml`. Each runs one program; they talk over Docker's private network
using the service name as hostname (e.g. `api:8000`, `postgres:5432`).

| Container | Image | Role | Exposed |
|---|---|---|---|
| `caddy` | `caddy:2` | HTTPS front door: gets a Let's Encrypt certificate for `BOT_DOMAIN`, forwards to `api:8000`. Only started with `--profile prod`. | 80, 443 (public) |
| `api` | built from `Dockerfile` | FastAPI app + background worker + LangGraph graphs. Runs `alembic upgrade head` on start. | `127.0.0.1:8000` only |
| `postgres` | `postgres:16` | All state. Data in the `pgdata` volume. | `127.0.0.1:5432` only |
| `scheduler` | built from `Dockerfile` (`python -m app.scheduler`) | Timed jobs on the America/Montreal timetable (`app/scheduler.py`); starts after `api` is healthy (migrations done) | none |

Why separate: only Caddy faces the internet; data outlives app rebuilds; each part restarts or
updates on its own; Postgres and Caddy use official images.

Ports bound to `127.0.0.1` can't be reached from outside the machine. Docker bypasses `ufw`,
so this binding (not the firewall) is what keeps Postgres and the API private.

**One API process only.** The background worker lives inside the `api` process (no Redis/Celery),
so uvicorn must run with a single worker.

## Request path: owner → app

```
Owner's WhatsApp → Meta servers
  → POST https://bot.<domain>/webhook   (JSON body + X-Hub-Signature-256 header)
  → DNS A record bot.<domain> → VPS IP
  → VPS port 443 → caddy (TLS) → reverse_proxy api:8000
  → FastAPI route POST /webhook (app/main.py)
      1. verify signature (WHATSAPP_APP_SECRET) → 401 if bad
      2. parse JSON → list of messages (app/whatsapp/parse.py)
      3. drop senders not in the owners allowlist (audit: ignored_non_owner)
      4. INSERT into inbound_messages ON CONFLICT (message_id) DO NOTHING  → dedupe
      5. wake the worker, return 200
  → Worker (app/worker.py) processes stored messages → Handler (app/handlers.py)
```

The webhook does no real work, so Meta always gets a fast 200. If the DB insert fails, the
request returns 500 and Meta retries later; dedupe makes the retry safe.

## Request path: app → owner

Outgoing messages are ordinary HTTPS calls from `api` to
`https://graph.facebook.com/<version>/<phone_number_id>/messages`, authenticated with
`WHATSAPP_ACCESS_TOKEN`. No inbound port is needed. Meta then sends delivery reports
(`statuses`) back to the webhook; failed ones are logged.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Runs `SELECT 1`; used by Docker healthcheck and the uptime monitor |
| GET | `/webhook` | Meta's one-time verification (`hub.mode`, `hub.verify_token`, `hub.challenge`) |
| POST | `/webhook` | All WhatsApp events |

## Startup sequence (`create_app` lifespan)

1. Create the SQLAlchemy engine/sessionmaker.
2. Sync the `owners` table from `OWNER_PHONES`.
3. Open a psycopg pool and set up LangGraph's `AsyncPostgresSaver` (creates checkpoint tables).
4. Build the WhatsApp client (or use an injected channel in tests), the graphs and the handler.
   Graphs are registered by the approval `item_type` they own (`{item_type: GraphEntry(graph)}`),
   so a button tap or an edit is routed to the right graph.
5. Start the worker: reset messages stuck in `processing`, resume approvals that were decided
   but never delivered to their graph, then start the processing loop.

## Local vs server

| | Local (Mac) | Server (OVHcloud VPS, Beauharnois QC) |
|---|---|---|
| Containers | `api`, `postgres` | `api`, `postgres`, `caddy` (`--profile prod`) |
| Public URL | `cloudflared` quick tunnel (changes each run) | `https://bot.<domain>` |
| HTTPS | Cloudflare | Caddy + Let's Encrypt |

Everything from the FastAPI route onward is identical.

## Code layout

| Module | Responsibility |
|---|---|
| `app/main.py` | App factory, routes, startup/shutdown |
| `app/config.py` | Settings from env |
| `app/deps.py` | `Deps` bundle (settings, sessionmaker, channel) passed to graphs/handlers |
| `app/channel.py` | `Channel` interface so WhatsApp can be swapped |
| `app/whatsapp/` | Signature, parsing, sending, button ids |
| `app/worker.py` | Queue processing |
| `app/handlers.py` | What the bot does with each owner message |
| `app/approvals.py` | Approval records and transitions |
| `app/graphs/` | LangGraph workflows |
| `app/owners.py` | Allowlist |
| `app/leads.py` | Lead dedupe and status transitions |
| `app/suppression.py` | Do-not-email list |
| `app/runs.py` | Run tracking, failure alerts |
| `app/jobs/` | Job registry (`run_job`); `lead_research.py` (daily research job) |
| `app/tools/` | Shared external APIs used across features (GitHub issues, `web.py` page fetcher; later Gmail, Calendar…). A source used by one feature lives with it (e.g. `app/permits/montreal.py`: client + mapping) |
| `app/redact.py` | Strip emails/phones from outgoing technical text |
| `app/llm.py` | The only way to call an LLM (OpenRouter) |
| `app/scoring.py` | Lead scoring step (cheap model + rubric) |
| `app/research.py` | Lead research step (agent loop: web search + `fetch_page`) |
| `app/guards/` | Code checks the model can't skip (`consent.py`: CASL check of a researched contact) |
| `app/permits/` | Permit model, filters, permits → leads pipeline |
| `app/scheduler.py` | Scheduler process |
| `app/skills/` | Versioned playbooks injected into LLM steps (`lead_scoring.md`, `lead_research.md` v1) |
| `app/db/` | Models, session, audit helper, migrations |
