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

## Planned (Phase 1)
Outreach graphs (ingest, research, draft, send, replies) and the scheduler — CLAUDE.md §7.
