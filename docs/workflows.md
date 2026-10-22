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
            → edit_text / edit_ai                     → record how this owner will edit
text        → owner owes an edit?                     → finish the edit
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

Graphs are registered by approval `item_type` (`Handler.graphs`, a `GraphEntry` each). For items the
AI can revise (`ai_edits=True`), Edit answers with two buttons, **My own text** (`edit_text`) and
**Tell AI changes** (`edit_ai`); the choice is stored as `payload.edit_mode` (only for the owner who
tapped Edit, same version), and text typed before choosing re-asks the choice. The edit decision is
`{action: edited, mode: text|ai, text, approval_id, by, by_name}`. Another owner tapping while it's
being edited gets `<name> is editing this draft.`

Every transition is one SQL statement:
`UPDATE approvals SET status=…, version=version+1 … WHERE id=:id AND status=:expected AND version=:v`.
Only one tap can match, so **the first owner wins**. A losing tap gets
`Already handled by <name> (<status>)`. A tap with an old version (from an earlier message) also
loses. If the same owner's tap is redelivered after a crash, the decision is re-delivered to
the graph instead.

After a winning Approve/Reject (or the edit text), the handler resumes the graph:
`graph.ainvoke(Command(resume=decision), {"configurable": {"thread_id": run_id}})`, then sets
`resumed_at`. What happens depends on the graph's saved state (`graph.aget_state`):

| Saved state | Action |
|---|---|
| Paused in an `interrupt()` whose value has **this** `approval_id` | Resume with the decision |
| Stopped between steps, no pause (crash after the decision was delivered) | Continue the run with `ainvoke(None)`: no decision, so none can reach a later pause |
| Finished, missing, or paused on a newer approval (edit loop) | Only mark delivered: resuming a missing thread would restart it from the beginning, and an old decision must never answer a newer approval |

Every graph call for a run (start, state check + resume) holds that run's `asyncio.Lock`
(`Handler._run_lock`). Owners' lanes run in parallel; without the lock, a tap that arrives while
the other lane is still inside the step that sent the buttons would see the run as "stopped
between steps" and run it twice. The API is a single process (CLAUDE.md §12), so an in-process
lock is enough; graphs started by the scheduler (#47) must pause before their buttons go out.

An edit decision carries the WhatsApp `message_id` that delivered it. If the resume fails after the
edit was saved, the worker's retry of that same message finds the unresumed edit
(`approvals.unresumed_edit_for_message`) and finishes the resume instead of treating the text as a
new message. Choosing the edit mode is audited (`approval_edit_mode`).

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
| `outreach_draft` | 08:00 weekdays (skips Québec holidays) | Qualified leads → one email draft per company → owners on WhatsApp (`app/jobs/outreach_draft.py`) |

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
   at most `RESEARCH_MAX_PER_DAY` (15, decided 2026-10-22) per Montréal calendar day, and stop once
   the day's spend reaches `RESEARCH_MAX_USD_PER_DAY` ($1.50; checked before each lead, failed
   attempts included). Manual re-runs share both budgets (from `research_details.researched_at`
   and `cost_usd`).
2. **Research** (`app/research.py`): `LLM.agent` with the playbook `app/skills/lead_research.md`
   (who: on a commercial fit-out the business named in the permit as moving in / operating (`tenant`) first, then
   developer/owner → general contractor → property manager → architect/designer as last resort;
   valid sources: company's own website or a public business directory, never social networks;
   free-mail addresses allowed there). The answer includes `link_source_url`: the page tying the
   company to the address, for owners to check at approval (null when the permit names the company).
   Tools: OpenRouter web search (Exa, ≤ `RESEARCH_MAX_SEARCHES`) and `fetch_page`
   (`app/tools/web.py`); ≤ `RESEARCH_MAX_TURNS` model turns; must end with a `ResearchOutcome`.
   The permit address is sent (needed to search); page text is wrapped in `<page>` blocks,
   angle brackets stripped, marked as untrusted data. The answer is cleaned before validation:
   blank or placeholder values ("Unknown", "Not found", "") → null; free-text roles mapped to the
   role list ("Entrepreneur général" → `general_contractor`, unmatched → null).
3. **Consent check** (`app/guards/consent.py`, code only): re-fetch `source_url` and require the
   email on the page, no "no solicitation" notice, not a social network or a search engine's page
   (`SEARCH_ENGINE_DOMAINS`, except Google Sites: an index is not where the email was published), the business named on
   the page (anywhere on its own site, since the model chose `website`; within ±200 characters of
   the email on a directory, where an address that doesn't carry the business name (e.g. a
   personal one) passes with `needs_owner_check` for the owners to confirm at approval; every
   distinctive word of a 1–2 word name, a majority otherwise, trade words like "Rénovations" or
   "Properties" not counted), and a company-domain email matching the website's domain.
   `mailto:` and `[at]`/`[dot]` addresses count as published and appear in the excerpt.
   Then the suppression list (`app/suppression.is_suppressed`). A fetch that fails for a passing
   reason (network, timeout, 5xx, 408/429) is `retryable`: handled like an error in step 5.
4. **Write** (version-checked, audited): passed → `new → researched → qualified` with email, phone,
   contact, `consent_*` columns; otherwise `no_valid_contact` (or `do_not_contact` if suppressed)
   **without the email** (Law 25: collect the minimum). Company name, website and relevance note are
   kept either way. Everything else goes to `leads.research_details`: model, turns, searches, cost,
   tool calls (final answer's arguments dropped), the answer without email/phone/name, the consent
   check with evidence (final URL, fetch time, HTTP status, page title, SHA-256; the excerpt around
   the email only for qualified leads, since it contains the address), and `same_company_lead_ids` when another lead has the same email domain (one developer,
   several permits — drafting must send one email per company).
5. **Errors** (model/API, or a retryable consent fetch): the lead stays `new` and is retried another
   day; after 3 attempts → `no_valid_contact`. Consent fetch errors are counted apart
   (`consent_fetch_errors`) and never fail the run. If every picked lead errors (or no LLM key while leads wait), the run fails
   and owners are alerted.

Run counts: `researched`, `qualified`, `not_found`, `consent_failed`, `suppressed`,
`research_errors`, `consent_fetch_errors`, `research_conflicts`, `daily_cap_reached`, `daily_spend_cap_reached`,
`web_searches`, `llm_tokens`,
`llm_cost_microusd`.

### Page fetcher (`app/tools/web.py`)
The model chooses URLs, so the fetcher is the safety boundary: http(s) only; hosts resolving to
private, loopback, link-local or reserved addresses refused (re-checked on each redirect, max 5),
and the address actually connected to is checked again (DNS rebinding; skipped behind an HTTP
proxy, which resolves names itself); social networks refused; 15 s per read and 30 s per page,
2 MB cap, HTML/plain text only, charset from the header or `<meta charset>`, no cookies or
JavaScript, identifying User-Agent. Returns text (scripts/styles removed), emails (incl. `[at]`/`(dot)` and
`mailto:`), links and a SHA-256 of the text.

## LLM calls (`app/llm.py`)
One client for every model call: OpenRouter chat API, model per step in `routes()` (overridable by
settings), fallbacks via OpenRouter `models` (sent together with `model`: OpenRouter rejects a
web-search request that names its model only in `models`), strict JSON-schema structured output validated by a
Pydantic model (one retry with the validation error), timeouts and retries (3 tries on 408/429/5xx;
a 429 waits until the rate-limit window resets, max 65 s), zero data retention (`provider.data_collection=deny`, `zdr=true`), usage and cost returned to the caller.

Each route can omit `temperature` (`None`) and set `reasoning_effort`: `email_draft`
(claude-sonnet-5.5) sends no temperature — no zero-retention provider accepts it — and
`reasoning: {effort: low}` (reasoning can't be disabled; default reasoning cost ~2.7× more and
cut off the JSON).

`agent()` is the bounded tool loop: each turn is one model call with our function tools, a required
`submit_answer` tool (arguments = the step's Pydantic model) and, while budget remains, OpenRouter's
`openrouter:web_search` server tool (engine `exa`, `max_uses` = searches left). Searches are read from
`usage.server_tool_use_details.web_search_requests`, capped at that call's `max_uses` (the count
includes refused, unbilled searches); once the budget is spent the tool is dropped. Tool errors and invalid
answers go back to the model as tool messages; the last turn forces `submit_answer`; no valid answer
→ `LLMError` carrying the usage already billed. Returns data, turns, searches, cost and the tool calls.

## Outreach drafts (`app/jobs/outreach_draft.py`, `app/graphs/outreach_email.py`, `app/drafting.py`)

```
08:00 job ─► per company: draft row + leads qualified→drafted ─► graph:
  compose (AI + guard, 1 retry) ─► request_approval ─► wait (interrupt) ─► apply
                                         ▲                                  │
                                         └── AI changes / refused edit ─────┤
         approve / own text OK → email_sends row (queued, or held in test mode) → END
         reject → leads disqualified · expire → leads back to qualified → END
```

**08:00 job** (Québec business days; `app/outreach/calendar.py`), in order:
1. Stop if `settings.outreach_paused`; fail (owners alerted) if the company identity is incomplete.
2. **Expire** drafts undecided for `OUTREACH_DRAFT_EXPIRY_BUSINESS_DAYS` (2): pending ones counted
   from when they were asked, ones an owner tapped Edit on but never finished from the Edit tap
   (otherwise they'd block every other draft). Approval rejected by `system` with decision `expire`
   (first action wins against an owner's tap), leads back to `qualified`.
3. **Finish stuck runs**: drafts still `drafting`/`pending` > 10 min whose run isn't paused are
   continued (`ainvoke(None)`) or started — except a run with an owner's decision not yet delivered
   (the API delivers it; running it here too would run the same step twice). Then **release stale links**: a `qualified` lead linked
   (`primary_lead_id`) to a lead that was never emailed (its draft expired, was rejected, blocked or
   failed) is unlinked, so the company is grouped and drafted again with all its permits.
4. **Pick** `qualified` leads with an email and consent URL, not linked to another lead:
   suppressed → `do_not_contact`; consent older than `OUTREACH_CONSENT_MAX_AGE_DAYS` (30) re-checked
   (`verify_consent`; failed → `no_valid_contact`, network error → another day). Grouped by company
   (company email domain, or the address for free-mail). A company already emailed or in progress
   (a lead in `drafted`…`won`/`lost`, or a non-cancelled `email_sends` row) isn't emailed again: the lead
   gets `primary_lead_id` and is left out. Siblings in a group point to its best-scored lead.
5. **Draft** up to the daily cap (`settings.outreach_daily_cap`, env `OUTREACH_DAILY_CAP`, minus drafts
   made today): draft row (`lead_ids` = the company's leads), leads `qualified → drafted`, run
   `outreach_email:<draft_id>` until it pauses. A model error marks the draft `blocked` and puts the
   leads back.
6. `present.morning`: owners are told how many drafts wait, then shown the first.

**Graph nodes** (each safe to re-run after a crash; `wait` only calls `interrupt()`):
- `compose`: `app/drafting.compose` — the model (route `email_draft`, Sonnet-class, reasoning effort
  low) gets the playbooks `brand_voice.md` + `casl_checklist.md` and the lead facts in a `<lead>`
  block (angle brackets stripped). Output `DraftEmail{subject, body}` → output guard; refused →
  one retry with the violations; refused twice → draft `blocked`, leads back to `qualified`, owners
  never see it.
- `request_approval`: approval `item_type=outreach_email`, `item_id=<draft_id>`. Nothing is sent here.
- `apply`:
  - **Approve** → if the address was suppressed or the lead closed since drafting, draft `cancelled`
    and nothing is queued; else render (`app/outreach/render.py`), final check (`check_rendered`), insert
    `email_sends` keyed `initial:<primary lead>:<mode>` (ON CONFLICT DO NOTHING), draft `approved`,
    leads `drafted → approved`, audit `outreach_queued`. In test mode (`EMAIL_MODE=test`) a recipient
    outside `EMAIL_TEST_RECIPIENTS` is `held` (never sent). **The graph never sends email** (#49).
  - **Own text** (`edit_text`): optional first line `Objet:`/`Subject:` replaces the subject, a
    greeting line is dropped; guard OK → saved as a new revision (`author=owner`, old text in
    `history`) and queued as written; refused → the owner is told why and the draft is re-asked.
  - **AI changes** (`edit_ai`): `drafting.revise` with the owner's instruction → guard → new revision →
    re-asked (new approval). Refused or model error → owner told, draft re-asked unchanged.
  - **Reject** → draft `rejected`, leads `disqualified` (`owner_rejected`). **Expire** → draft
    `expired`, leads back to `qualified`.

**Showing drafts** (`app/outreach/present.py`): one at a time — the oldest pending outreach approval,
to every owner whose 24 h window is open: a text with the research evidence (company, role, address
of every permit, why, where the email was found, the company↔project page, the `needs_owner_check`
excerpt when flagged) and the **whole email exactly as it will be sent** — greeting, signature and
CASL footer, the unsubscribe link shown as `[unsubscribe link]` — split, never cut, then Approve / Edit /
Reject (Approve reads "Approve (checked)" for flagged addresses). Nothing new is shown while an owner
is editing; an approval whose draft is no longer pending is skipped (logged), never blocking the rest. Called after every decision (`GraphEntry.after_decision`), on the template's
**Show drafts** button and on the `drafts` command. Morning: `drafts_ready` template for an owner
whose window is closed, else a text. Message keys include approval id + version: never repeated.

## Unsubscribe (`app/unsubscribe.py`, routes in `app/main.py`)
Every outreach email links to `<PUBLIC_BASE_URL>/u/<token>` (token = `email_sends.unsubscribe_token`,
128 bits, no personal data; the row is kept ≥ 60 days so the link keeps working, CASL) and carries
`List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers (RFC 8058).

| Request | What happens |
|---|---|
| `GET /u/<token>` | Page with one Confirm button, French first then English. **Never unsubscribes**: corporate mail scanners and previews open every link |
| `POST /u/<token>` | Applies the opt-out. Same URL the mail provider POSTs for one-click |
| Unknown / expired token | Same kind of page with Solecra's contact email; nothing changes, nothing is revealed |

`apply_unsubscribe` runs in **one transaction** (a crash can't leave it half applied; repeating it is
harmless): suppress the address and, for a company address, the whole domain (free-mail: the address
only; never `gmail.com`; a **test-mode** email, `email_sends.mode='test'`, opts out the address only,
so a tester on the company's own domain doesn't suppress it for good); matching leads → `do_not_contact` (exact match on domain or address, never
`LIKE`); `queued`/`held` emails to that address or domain → `cancelled` (an email already `sending`
can't be undone here: the sender re-checks suppression right before each Gmail call, #49); outreach
drafts involving those leads that wait for owners are closed: approval rejected by `system` with
decision `unsubscribed`, the graph resumed (it ends, draft `cancelled`). The audit log records the
domain only. **Races:** the leads are locked (`SELECT … FOR UPDATE`, in id order) and so is the lead
in `queue_send`, so an owner's Approve and an unsubscribe take turns: either Approve sees the
opt-out and queues nothing, or its email row is committed first and the opt-out then cancels it.
Owners get a WhatsApp message right away **only** if something pending was cancelled **and their 24 h
window is open** (free-form text outside it isn't delivered by Meta; it is logged instead); otherwise
it shows in the daily digest later (#12). The resume of closed drafts and the owner messages run
**after** the page is returned (a slow graph run or WhatsApp call never makes a mail provider wait;
`resume_unfinished` covers a crash). A button tapped on a draft the system closed answers
"Closed: the prospect unsubscribed." (or "nobody decided in time"). Any later lead of that company is moved to
`do_not_contact` by the 08:00 job (suppression is checked first).

## Output guard (`app/guards/output.py`)
`check_body(subject, body, ctx)` on every AI or owner text, `check_rendered` on the final email:

| Blocked | Why |
|---|---|
| Numbers not in the facts given to the model (address, units, permit text) or `OUTREACH_ALLOWED_NUMBERS` | §2 rule 4 |
| Price/cost words (`prix`, `coût`, `rabais`, `dollars`, `frais`, `taxes`, `$`, `%`, `price`, `fee`, `discount`…) | rule 3 |
| Duration words (`minute`, `heure`, `jour`, `semaine`, `délai`, `day`, `week`…) | rule 3 (labour time) |
| Measurements (`po`, `pouce`, `pied`, `cm`, `ft`, `36 x 48`, `²`…) | rule 3 |
| `OUTREACH_BLOCKED_TERMS` (supplier / brand names) | rule 3 |
| Links other than `COMPANY_WEBSITE` (any URL, `www.`, or bare domain such as `bit.ly/x`, `promo.xyz`), any email address | rule 5 (injected links), contact block is code's |
| Placeholders/markup (`[`, `{`, `<`, `xxx`, `lorem`), greeting or sign-off in the body | code adds them |
| Not the lead's language (function-word check on the body, and on the subject when it has telling words) | Bill 96 |
| Subject > 90 or body < 40 / > 1,500 characters | |

The final email must carry every identity field, the unsubscribe link and the List-Unsubscribe
headers; otherwise nothing is queued (the run fails and alerts).

## Planned (Phase 1)
Gmail sending (#49), replies (#11), Langfuse tracing (#44),
owner chat and digest (#12) — CLAUDE.md §7.
