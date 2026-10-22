# Security and compliance

Status of each non-negotiable rule from CLAUDE.md §2: where it is enforced, or when it will be.

| # | Rule | Status | Where |
|---|---|---|---|
| 1 | Owner approval before anything leaves / touches money | Mechanism built | `approvals` + `interrupt()`; nothing external is sent yet |
| 2 | Least privilege (own tools, own DB role) | Planned | Separate DB roles when outreach/ERP land |
| 3 | Estimate confidentiality + output guard | Planned | `app/guards/` (Phase 1 step 12, ERP in Phase 2) |
| 4 | Never invent facts | Planned | Drafting prompts + output guard |
| 5 | Untrusted input is data | Partly | Scoring: city description wrapped in `<permit>` tags, angle brackets stripped, structured output only, no tools. Research/replies later |
| 6 | Owner allowlist | **Enforced** | Webhook drops non-owners before storing (`app/main.py`); handler re-checks (`app/handlers.py`) |
| 7 | CASL | Partly | Consent evidence columns on `leads` and `suppression` table + `is_suppressed` (step 8); evidence capture (step 10), send-time checks (step 12) |
| 8 | Bill 96, French first | Partly | Demo draft is French; `leads.language` defaults to `fr` |
| 9 | Law 25 | Partly | Data hosted in Québec (OVHcloud BHS); non-owner messages not stored; retention job planned |
| 10 | No secrets in repo | **Enforced** | `.env` git-ignored; `.env.example` has no values |
| 11 | Idempotency | **Enforced** | `inbound_messages` PK, `outbound_messages` keys, versioned approvals |

## Other protections in place
- **Webhook authenticity:** HMAC SHA-256 signature on every POST; unset secret never passes.
- **Network exposure:** only Caddy (80/443) and SSH are public; API and Postgres bind to
  `127.0.0.1`. Server: SSH keys only, `ufw`, unattended security upgrades (RUNBOOK §6).
- **Container user:** the app runs as non-root `solecra` (uid 1000).
- **Audit trail:** approvals, owner sync, ignored senders, demo outcomes → `audit_log`.
- **Logs:** non-owner numbers are masked to the last 4 digits.
- **GitHub issues from agents:** technical details only; emails and phone numbers redacted
  (`app/redact.py`). Token limited to Issues on this repo.
- **Suppression audit:** only the domain is logged, not the address.
- **LLM calls:** zero data retention requested on every call (`app/llm.py`); scoring sends permit facts
  only, never addresses or contact data.

## Known gaps
- Owner message text is logged in full (fine for Gate 1; review before real client data flows).
- WhatsApp access token lives in `.env` on the server; use a system-user token with minimal
  permissions in production.
