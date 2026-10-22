# Data model

All tables live in Postgres and are created by Alembic migrations in
`app/db/migrations/versions/`. Models: `app/db/models.py`. Never change the schema by hand.
Alembic only manages tables defined in the models (`include_name` in `env.py`), so autogenerate
never touches LangGraph's checkpoint tables.

## Gate 1 tables (migration `0001`)

### `inbound_messages` — webhook dedupe + work queue
| Column | Notes |
|---|---|
| `message_id` PK | Meta's `wamid...`; a redelivered webhook hits the PK and is ignored |
| `sender` | Phone, digits only |
| `msg_type` | `text` \| `button` \| `other` |
| `text`, `button_id` | Message text, or tapped button id |
| `payload` | Raw message JSON |
| `received_at` | Insert time; processing order |
| `status` | `pending` → `processing` → `done` \| `failed` |
| `attempts`, `last_error`, `processed_at` | Retry bookkeeping (max `WORKER_MAX_ATTEMPTS`) |

Only messages from owners are stored.

### `outbound_messages` — WhatsApp send dedupe
| Column | Notes |
|---|---|
| `idempotency_key` PK | See keys below; a key already `sent` is never sent again |
| `recipient`, `payload` | Who, and the exact Graph API body |
| `status` | `pending` \| `sent` \| `failed` |
| `wa_message_id`, `error`, `created_at`, `sent_at` | Result |

### `owners` — allowlist
`phone` PK, `name`, `role`, `active`, `last_inbound_at` (start of the 24 h window).
Synced from `OWNER_PHONES` at each start; owners removed from env become `active=false`.

### `approvals`
| Column | Notes |
|---|---|
| `id` PK | Used in button ids |
| `run_id` | LangGraph `thread_id` of the waiting workflow |
| `item_type`, `item_id` | What is being approved (`demo_draft` for now) |
| `status` | `pending` → `approved` \| `rejected` \| `editing` → `edited` |
| `version` | +1 on every transition; buttons carry the version they were sent with |
| `decided_by`, `decided_at` | Owner phone and time |
| `payload` | Item data (e.g. `draft`) + `decision` once decided |
| `resumed_at` | Set after the graph received the decision; null + final status = must resume |

### `audit_log`
`actor` (phone or `system`), `action`, `entity`, `entity_id`, `details` (JSON), `created_at`.
Written for: owner sync, ignored non-owners, demo start, approval requested/decided, demo outcome.

### `settings`
`key` PK, `value` (JSON), `updated_by`, `updated_at`. Keys listed at the end of this page.

### LangGraph checkpoint tables
`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`. Created by
`AsyncPostgresSaver.setup()` at startup, **not** by Alembic. Hold paused workflow state.

## Idempotency keys (`outbound_messages.idempotency_key`)
| Key | Sent when |
|---|---|
| `reply:<inbound message_id>` | Direct reply to an owner message (echo, help, errors) |
| `approval:<id>:v<version>:ask:<phone>` | Approval request with buttons |
| `approval:<id>:result:<phone>` | Final outcome of a demo approval |

## Outreach tables (migrations `0002`–`0005`)

### `leads`
| Column | Notes |
|---|---|
| `id` PK | |
| `business_name`, `contact_name`, `role`, `email`, `phone`, `address`, `website` | Contact data; collect the minimum (Law 25). `email` stored lowercase |
| `source`, `source_ref` | Where it came from (`permit`, `owner_tip`, `google_places`, `news`, `manual`) and its id there; **unique together** |
| `score` | Fit score 0–100 from the scoring step (`app/scoring.py`); below `LEAD_SCORE_THRESHOLD` → `disqualified` |
| `status` | See flow below |
| `language` | `fr` (default) or `en` — Bill 96, French first |
| `consent_source_url`, `consent_captured_at`, `consent_role`, `relevance_note` | CASL consent evidence, set by the research step only when the consent check passes (`app/guards/consent.py`) |
| `score_reason` | One line from the scoring step (why this score) |
| `primary_lead_id` | FK to the lead of the same company that gets the email (one email per company); null on the primary itself |
| `research_details` | Research step record (`app/jobs/lead_research.py`): `researched_at`, `attempts`, model, turns, searches, cost, tool calls, answer (no email/phone/name), `consent_check` with evidence (URL, fetch time, page hash, excerpt), `same_company_lead_ids`, `result` |
| `source_details` | Public facts from the source record (permit work/building type, units, issue date, description). Permits: later permits at the same address in `permit_ids` and `other_permits` (newest last, max 10). **No address/coordinates/personal data** — those stay in their own columns |
| `owner_assigned` | Owner phone, optional (leads are shared) |
| `email_domain`, `phone_normalized`, `address_normalized` | Dedupe keys, computed in `app/leads.py`, indexed |
| `created_at`, `updated_at`, `version` | `version` +1 on every status change (optimistic locking) |

**Dedupe** (`app/leads.insert_lead`): same `(source, source_ref)`, or same company email domain,
normalized phone, or normalized address → existing lead returned, nothing inserted.
- Free-mail domains (`gmail.com`, `videotron.ca`, …) never match.
- Phone: digits, 10-digit numbers get a leading `1`.
- Address: lowercase, no accents/punctuation, FR/EN street words unified (`boulevard/blvd → boul`,
  `ouest/west → o`, `saint → st`), postal code joined, filler words (`de`, `du`, `qc`…) dropped,
  words sorted (so `10 rue Ontario Est` = `10 Ontario Street East`).

**Status flow** (`app/leads.transition`, checked against `allowed_transitions`):
```
new → researched → qualified → drafted → approved → sent → follow_up_1 → follow_up_2
                                  ↑  ↓         ↓        └──────────┴────────────┴──→ replied
                              (draft rejected / changes)                               ↓
                                                         site_visit_booked ⇄ replied → won | lost
any non-final → disqualified | no_valid_contact        any → do_not_contact        lost → replied
```
Each transition: `UPDATE … WHERE id AND version AND status` (first writer wins), audited as `lead_status`.
One way back from an exit: `app/leads.reopen` (`disqualified → new`, version-checked, audited), used
only by the permits pipeline when a new permit arrives for a lead its score disqualified.

### `touches`
`lead_id` FK (cascade), `direction` (`in`/`out`), `type` (`initial`/`follow_up`/`reply`/`owner_response`),
`gmail_message_id` (unique), `gmail_thread_id` (indexed — replies are matched by thread), `subject`,
`body`, `sent_at`, `status` (`pending`/`sent`/`received`/`failed`), `email_send_id` (FK to
`email_sends`), `rfc_message_id` (for `In-Reply-To` on follow-ups). Used from step 12.

### `email_drafts` (migration `0005`)
The outreach email to one company. `lead_id` (primary lead) and `lead_ids` (all the company's leads),
`run_id` (LangGraph thread), `kind` (`initial`; `follow_up` in step 13), `language`, `revision`,
`author` (`ai`/`owner`), `subject`, `body` (current text), `status`
(`drafting`/`pending`/`approved`/`rejected`/`blocked`/`expired`/`cancelled`), `guard` (last output-guard
result), `llm` (model, cost), `history` (earlier revisions). Status meanings: `blocked` = the guard
refused it twice or drafting failed (owners never saw it); `expired` = undecided for 2 business days;
`cancelled` = approved after the address was suppressed or the lead closed, so nothing was queued. Partial unique index: **at most one open initial
draft per lead** (`status` in `drafting`/`pending`/`approved`).

### `email_sends` (migration `0005`)
Gmail send queue, like `outbound_messages` for WhatsApp: the primary key is an **idempotency key**
(e.g. `initial:<lead_id>:<mode>`), so an email is queued, and sent, at most once. `draft_id`,
`revision`, `lead_id`, `mode` (`test`/`live`), `intended_to`, `actual_to`, `subject`, `rendered_text`,
`content_sha256` (of the approved text, re-checked at send), `unsubscribe_token` (unique),
`rfc_message_id` (unique), `status` (`queued`/`held`/`sending`/`sent`/`unknown`/`failed`/`cancelled`),
`attempts`, `error`, `approved_by`, `gmail_message_id`, `gmail_thread_id`, `sent_at`. Lead and draft
FKs are `SET NULL`: rows outlive a purged lead so unsubscribe links keep working ≥ 60 days (CASL).

### `suppression`
`email_or_domain` PK (lowercase address or bare domain), `reason` (`unsubscribe`/`bounce`/`do_not_contact`),
`created_at`. `app/suppression.is_suppressed(email)` checks the address **and** its domain; must be
called before every send. Entries are never removed by code. The audit log records only the domain.

### `runs`
`job`, `started_at`, `finished_at`, `status` (`running`/`succeeded`/`failed`), `counts` (JSON, e.g.
`{"inserted": 12}`), `error` (`Type: message`). Index on `(job, started_at)`. See `docs/workflows.md`.

## Other keys in `settings`
| Key | Value |
|---|---|
| `agent_issue:<job>` | `{"number": N}` — GitHub issue opened for that job's repeated failures |
| `outreach_paused` | `true` stops drafting and sending (`app/settings_store.py`) |
| `outreach_daily_cap` | New outreach emails per day; falls back to `OUTREACH_DAILY_CAP` |
| `email_next_send_at` | ISO time: no outreach email before it (random spacing); read back as UTC if stored without an offset |

Runtime settings are read and written with `app/settings_store.py`; every write is audited
(`setting_changed`).
