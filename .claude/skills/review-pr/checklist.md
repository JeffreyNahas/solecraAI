# Review checklist (priority order)

Every finding needs a concrete failure scenario. Skip items the PR doesn't touch.

## 1. Compliance and safety (CLAUDE.md §2) — usually blocking
- **Owner approval:** nothing reaches a prospect/client or touches money without an approval
  (`interrupt()` + approvals row). Only the listed protective exceptions skip it.
- **Least privilege:** new code paths only use the tools/DB access they need; the orchestrator
  can't send emails or post accounting entries itself.
- **Estimate confidentiality:** client-facing code never reads `estimates_internal`; output guard
  applied to outbound text.
- **No invented facts:** prices, availability, measurements, counts in owner/client messages come
  from DB/tool values, never from model text.
- **Untrusted input:** web pages, prospect emails, forwarded posts are passed as data; no tool calls
  or behaviour changes driven by their content.
- **Allowlist:** only owner numbers can trigger actions.
- **CASL:** consent evidence required before a first cold email; identification, contact info and
  a working unsubscribe in every email; suppression checked before every send.
- **Bill 96:** client-facing text French first unless the contact's language is `en`.
- **Law 25:** minimum data collected; no personal data (emails, phones, names, addresses) in logs,
  GitHub issues/comments, or error messages that leave the system (`app/redact.py`).
- **Secrets:** none in code, tests, fixtures, docs or commit messages; new settings documented in
  `.env.example` without values.
- **Idempotency:** inbound dedupe by message id; every outbound send has a stable idempotency key;
  state changes are versioned (first writer wins).

## 2. Correctness
- **Two owners in parallel:** no assumption about the order of messages from different owners;
  concurrent taps/edits resolved by versioned updates.
- **Restarts and replays:** LangGraph re-runs an interrupted node from the top — no side effects in
  or before `interrupt()` in the same node; work left half-done after a crash is recovered.
- **Retries:** a retried webhook, job or API call can't double-send, double-insert or double-charge.
- **Error handling:** external calls go through the wrapper (timeouts, retries, logging); failures
  in alerting/logging never hide the original error.
- **Edge cases:** empty/None inputs, unicode/accents (French), time zones (`America/Montreal` vs
  UTC), the WhatsApp 24 h window, Meta payload variants.

## 3. Data and migrations
- Migration created with Alembic, matches the models, applies on an existing DB with data,
  `downgrade()` works; no destructive change without a data plan.
- Alembic doesn't touch LangGraph checkpoint tables.
- New columns/indexes match the queries that use them; unique constraints where dedupe relies on them.

## 4. Tests
- New behaviour and every fixed bug have a test that would fail without the change.
- No reliance on timing or ordering between owners; new tests stable over 5 runs.
- External APIs mocked; DB tests use the real Postgres fixture.

## 5. Docs and operations
- `docs/` pages for the touched areas describe the new behaviour (`docs/doc-map.toml`).
- RUNBOOK updated for anything an operator runs or configures; `.env.example` has new settings.
- CLAUDE.md layout/data model/status still true.

## 6. LLM cost (from step 9)
- No model calls inside unbounded loops; daily caps respected; cheap model for bulk steps;
  token/cost recorded on the run.

## 7. Performance (lowest priority at this scale)
- N+1 queries in loops over leads/messages; unbounded queries or memory growth; blocking calls in
  async code.
