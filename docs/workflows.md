# Workflows

## Background worker (`app/worker.py`)

Processes `inbound_messages` after the webhook has stored them.

- **Wake-up:** the webhook calls `worker.notify()`; the loop also polls every
  `WORKER_POLL_SECONDS` as a safety net.
- **Lanes:** one asyncio task per sender. A sender's messages are handled **one at a time, oldest
  first**; different owners run in parallel.
- **Claiming:** `SELECT ... FOR UPDATE SKIP LOCKED`, then `status='processing'`, `attempts+1`.
- **Result:** `done`; on exception back to `pending` with a growing delay, or `failed` after
  `WORKER_MAX_ATTEMPTS`.
- **Startup recovery:** rows left `processing` by a crash go back to `pending`; approvals with a
  final status but no `resumed_at` are resumed (`Handler.resume_unfinished`).

## Message handling (`app/handlers.py`)

```
button tap  → parse "<action>:<approval_id>:<version>" → approval transition (below)
text        → owner owes replacement text for an Edit?  → finish the edit
            → "test draft"                            → start approval_demo
            → "help" / "aide" / "?"                   → command list
            → anything else                           → "Echo: <text>"   (Gate 1 test mode)
other types → "I can only read text messages for now."
```

## Approval lifecycle (`app/approvals.py`)

```
            tap Approve            ┌──────────┐
 pending ─────────────────────────►│ approved │
    │       tap Reject             ├──────────┤
    ├─────────────────────────────►│ rejected │
    │       tap Edit               ├──────────┤
    └──► editing ── owner text ───►│  edited  │
                                   └──────────┘
```

Every transition is one SQL statement:
`UPDATE approvals SET status=…, version=version+1 … WHERE id=:id AND status=:expected AND version=:v`.
Only one tap can match, so **the first owner wins**. A losing tap gets
`Already handled by <name> (<status>)`. A tap with an old version (from an earlier message) also
loses. If the same owner's tap is redelivered after a crash, the decision is re-delivered to
the graph instead.

After a winning Approve/Reject (or the edit text), the handler resumes the graph:
`graph.ainvoke(Command(resume=decision), {"configurable": {"thread_id": run_id}})`, then sets
`resumed_at`.

## Graph: `approval_demo` (`app/graphs/approval_demo.py`)

```
START → make_draft → request_approval → wait_for_owner → report → END
```

| Node | Does |
|---|---|
| `make_draft` | Fixed French demo text (no LLM) |
| `request_approval` | Creates (or reuses) the `approvals` row, sends buttons to **all active owners** |
| `wait_for_owner` | `interrupt()` only; pauses here, state saved in Postgres |
| `report` | Tells all owners the outcome; nothing is sent to anyone outside |

`thread_id = run_id = "approval_demo:<uuid>"`. On resume LangGraph re-runs the interrupted node
from the top, so `wait_for_owner` has no side effects and every send has an idempotency key.

## Jobs and run tracking (`app/jobs/`, `app/runs.py`)

A **job** is `async def job(deps, run)`; it reports counts with `run.add("inserted", n)` and fails by
raising. Jobs are registered by name in `app/jobs.JOBS`; `run_job(deps, name)` runs one with tracking
and never raises. Today: `selftest_ok`, `selftest_fail` (forced failure for testing alerts).
Run one by hand: `uv run python -m scripts.run_job <name>` (server: `docker compose exec api python -m scripts.run_job <name>`).

`track_run(deps, job)` writes a `runs` row (`running` → `succeeded`/`failed`) and then:

| Situation | Owners (WhatsApp) | GitHub |
|---|---|---|
| First failure after a success | One plain-language alert (`⚠️ <job> didn't work…`), key `run:<id>:alert:<phone>` | — |
| More failures in the same streak | nothing (no spam) | from `RUN_FAILURE_ISSUE_THRESHOLD` failures in a row: open **one** issue `[agent] <job> failing` (label `bug`), then comment on it each time |
| Success after failures | `✅ <job> is working again.` | comment "succeeded again" (issue stays open until a human closes it) |

- Issue number per job is stored in `settings` (`agent_issue:<job>`). If that issue was closed, the
  next threshold breach opens a new one.
- Issue text has the job, run id, local time, streak and the error with emails and phone numbers
  replaced (`app/redact.py`). No lead data.
- Reporter (`app/tools/github_issues.py`) is off when `GITHUB_ISSUES_TOKEN` is empty: it only logs.
- Alerting errors are logged and never replace the job's own error.
- Owner labels for jobs: `JOB_LABELS` in `app/runs.py`.

## Scheduler (`app/scheduler.py`, container `scheduler`)
Runs registered jobs on the timetable in `SCHEDULE` (cron, **America/Montreal**). One instance per
job at a time; after downtime a missed slot runs once (not once per missed slot), within 1 h.
Jobs listed but not yet registered in `app/jobs.JOBS` are skipped with a warning.

| Job | When | Status |
|---|---|---|
| `permits_ingest` | 06:00 daily | Montréal permits → scored leads (`app/permits/`) |
| `lead_research` | 07:00 daily | Qualified leads → company + published contact + consent evidence (`app/jobs/lead_research.py`) |

## Permits → leads (`app/permits/`, `app/scoring.py`)
```
city permits ─► Permit (our model) ─► filters ─► insert_lead (dedupe) ─► score (cheap LLM) ─► keep | disqualify
```
1. **Source → `Permit`** (`app/permits/model.py`): our own categories (`WorkCategory`,
   `BuildingCategory`), so a change in the city's format touches one mapping function.
2. **Filters** (`app/permits/filters.py`, decided 2026-10-22): keep new construction, major
   renovation, change of use, commercial fit-out, condo conversion; on multi-residential with
   ≥ `PERMITS_MIN_UNITS` units, commercial, office or institutional buildings; within
   `PERMITS_MAX_KM` of downtown (haversine; permits without coordinates are kept). Single-family
   homes are skipped (no contact data → no CASL-compliant email).
3. **Insert** as `source=permit`, `source_ref=<permit id>`, status `new`, language `fr`, with
   `source_details`. Same permit → `duplicates`. **Another permit at an address that already has a
   permit lead** (decided 2026-10-22): added to that lead (`source_details.other_permits`,
   `permit_ids`); if the lead is still `new` or was disqualified **by its score**, its score is cleared
   (`disqualified → new` via `app/leads.reopen`) and it is re-scored with every permit at the address
   (`rescored`); leads further along just keep the permit (`permits_added`).
4. **Score** (`app/scoring.py`): cheap model via `app/llm.py`, rubric `app/skills/lead_scoring.md`,
   output `{score 0–100, reason}`. Only the permit's public facts are sent; the address and coordinate
   fields are not. The city's free-text description is sent as-is (it can mention a civic number or a
   business name) and is wrapped in `<permit>` tags as data. Up to `SCORING_MAX_PER_RUN` per run,
   including leads left unscored by an earlier failed run.
   **Failures alert:** if leads were waiting and **none** could be scored (no key, bad key, wrong
   model id, no zero-retention provider), the run fails, so owners get the failure alert and
   repeated failures open an `[agent]` issue. Inserted leads stay and are scored by the next run.
   Cost of failed calls is counted too.
5. **Threshold:** score < `LEAD_SCORE_THRESHOLD` → `disqualified` (audited with the score); otherwise
   the lead stays `new`, ready for research (#8). Score writes are version-checked.

Run counts: `seen`, `filtered_out` (+ `filtered_work|building|units_below_min|too_far`),
`inserted`, `duplicates`, `rescored`, `permits_added`, `permit_conflicts`, `scored`, `scoring_errors`, `above_threshold`, `below_threshold`,
`llm_tokens`, `llm_cost_microusd`.

### Source: City of Montréal open data (`app/permits/montreal.py`)
Dataset `permis-construction` (CC BY 4.0, published **weekly**), CKAN datastore resource
`5232a72d-…`, queried with the SQL API (POST). Only `CO` (construction) and `TR` (transformation)
are fetched; `CA` certificates and `DE` demolitions can't be leads. First run looks back
`PERMITS_LOOKBACK_DAYS`; later runs fetch from the **last successful run minus 14 days** (weekly
publication + overlap), so an outage of any length is caught up. Dedupe absorbs the overlap.
The city's firewall blocks bursts: one query per 1,000 rows, 2 s between pages, backoff 30 s / 2 min /
5 min on 403/429/5xx, identifying User-Agent.

Mapping (labels differ by borough): units are parsed as a **lower bound** from the building label
(`"5 à 11 Logements"` → 5, `Multifamiliale`/`Condominium` → 4, `Multiplex` → 2), `nb_logements`
used when larger. Work type from the city label + description keywords; plain transformations are
`major_renovation` and the scoring step judges how major they are.

Real dry run (2026-10-22, last 30 days): 1,045 fetched → 294 kept (157 multi-residential,
110 commercial, 21 institutional, 6 office); dropped 410 < 4 units, 295 other buildings, 46 minor work.

## Lead research (`app/research.py`, `app/jobs/lead_research.py`, `app/guards/consent.py`)
```
new, score ≥ threshold ─► agent loop (search + fetch_page) ─► answer ─► consent check (code) ─► qualified
                                                                   └─► no_valid_contact | do_not_contact
```
1. **Pick** permit leads still `new` with `score ≥ LEAD_SCORE_THRESHOLD`, highest score first,
   at most `RESEARCH_MAX_PER_DAY` (15, decided 2026-10-22) per Montréal calendar day. Manual re-runs
   share the budget (counted from `research_details.researched_at`).
2. **Research** (`app/research.py`): `LLM.agent` with the playbook `app/skills/lead_research.md`
   (who: developer/owner → general contractor → property manager → architect/designer as last resort;
   valid sources: company's own website or a public business directory, never social networks).
   Tools: OpenRouter web search (Exa, ≤ `RESEARCH_MAX_SEARCHES`) and `fetch_page`
   (`app/tools/web.py`); ≤ `RESEARCH_MAX_TURNS` model turns; must end with a `ResearchOutcome`.
   The permit address is sent (needed to search); page text is wrapped in `<page>` blocks,
   angle brackets stripped, marked as untrusted data.
3. **Consent check** (`app/guards/consent.py`, code only): re-fetch `source_url` and require the
   email on the page, no "no solicitation" notice, not a social network, the page is the company's
   site or names the business, and a company-domain email matches the website's domain.
   Then the suppression list (`app/suppression.is_suppressed`).
4. **Write** (version-checked, audited): passed → `new → researched → qualified` with email, phone,
   contact, `consent_*` columns; otherwise `no_valid_contact` (or `do_not_contact` if suppressed)
   **without the email** (Law 25: collect the minimum). Company name, website and relevance note are
   kept either way. Everything else goes to `leads.research_details`: model, turns, searches, cost,
   tool calls (final answer's arguments dropped), the answer without email/phone/name, the consent
   check with evidence (final URL, fetch time, HTTP status, page title, SHA-256, excerpt around the
   email), and `same_company_lead_ids` when another lead has the same email domain (one developer,
   several permits — drafting must send one email per company).
5. **Errors** (model/API): the lead stays `new` and is retried another day; after 3 attempts →
   `no_valid_contact`. If every picked lead errors (or no LLM key while leads wait), the run fails
   and owners are alerted.

Run counts: `researched`, `qualified`, `not_found`, `consent_failed`, `suppressed`,
`research_errors`, `research_conflicts`, `daily_cap_reached`, `web_searches`, `llm_tokens`,
`llm_cost_microusd`.

### Page fetcher (`app/tools/web.py`)
The model chooses URLs, so the fetcher is the safety boundary: http(s) only; hosts resolving to
private, loopback, link-local or reserved addresses refused (re-checked on each redirect, max 5);
social networks refused; 15 s timeout, 2 MB cap, HTML/plain text only, no cookies or JavaScript,
identifying User-Agent. Returns text (scripts/styles removed), emails (incl. `[at]`/`(dot)` and
`mailto:`), links and a SHA-256 of the text.

## LLM calls (`app/llm.py`)
One client for every model call: OpenRouter chat API, model per step in `routes()` (overridable by
settings), fallbacks via OpenRouter `models`, strict JSON-schema structured output validated by a
Pydantic model (one retry with the validation error), timeouts and retries (3 tries on 408/429/5xx;
a 429 waits until the rate-limit window resets, max 65 s), zero data retention (`provider.data_collection=deny`, `zdr=true`), usage and cost returned to the caller.

`agent()` is the bounded tool loop: each turn is one model call with our function tools, a required
`submit_answer` tool (arguments = the step's Pydantic model) and, while budget remains, OpenRouter's
`openrouter:web_search` server tool (engine `exa`, `max_uses` = searches left). Tool errors and invalid
answers go back to the model as tool messages; the last turn forces `submit_answer`; no valid answer
→ `LLMError` carrying the usage already billed. Returns data, turns, searches, cost and the tool calls.

## Planned (Phase 1)
Drafting and sending (#10), replies (#11), Langfuse tracing (#44),
owner chat and digest (#12) — CLAUDE.md §7.
