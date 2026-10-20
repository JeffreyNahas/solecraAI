# Data model

All tables live in Postgres and are created by Alembic migrations in
`app/db/migrations/versions/`. Models: `app/db/models.py`. Never change the schema by hand.

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
`key` PK, `value` (JSON), `updated_by`, `updated_at`. Created, not used yet (Phase 1: pause
outreach, daily cap).

### LangGraph checkpoint tables
`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`. Created by
`AsyncPostgresSaver.setup()` at startup, **not** by Alembic. Hold paused workflow state.

## Idempotency keys (`outbound_messages.idempotency_key`)
| Key | Sent when |
|---|---|
| `reply:<inbound message_id>` | Direct reply to an owner message (echo, help, errors) |
| `approval:<id>:v<version>:ask:<phone>` | Approval request with buttons |
| `approval:<id>:result:<phone>` | Final outcome of a demo approval |

## Planned (Phase 1, step 8)
`leads`, `touches`, `suppression` — see CLAUDE.md §6. Will be added here when migrated.
