# Security and compliance

Status of each non-negotiable rule from CLAUDE.md §2: where it is enforced, or when it will be.

| # | Rule | Status | Where |
|---|---|---|---|
| 1 | Owner approval before anything leaves / touches money | Mechanism built | `approvals` + `interrupt()`; nothing external is sent yet |
| 2 | Least privilege (own tools, own DB role) | Planned | Separate DB roles when outreach/ERP land |
| 3 | Estimate confidentiality + output guard | Planned | `app/guards/` (Phase 1 step 12, ERP in Phase 2) |
| 4 | Never invent facts | Planned | Drafting prompts + output guard |
| 5 | Untrusted input is data | Partly | Scoring: city description in `<permit>` tags, structured output, no tools. Research: page text in `<page>` blocks marked untrusted, angle brackets stripped; tools limited to search + `fetch_page`; the model's contact is only used if the code consent check passes (prompt-injection test in `tests/test_lead_research.py`; also passed live 3/3 with a planted page, 2026-10-22). Replies later |
| 6 | Owner allowlist | **Enforced** | Webhook drops non-owners before storing (`app/main.py`); handler re-checks (`app/handlers.py`) |
| 7 | CASL | Partly | Evidence capture + code check at research (`app/guards/consent.py`: published on the page, no "no solicitation" notice, company site or directory naming the business, suppression list; social networks and search-engine pages refused as sources); send-time checks (step 12) |
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
  only — not the address/coordinate fields or contact data. The city's public description is sent
  as-is and can mention a civic number or business name (public record).

- **Fetching the web** (`app/tools/web.py`): internal/private addresses refused on every hop and
  again on the connected socket (DNS rebinding), which protects the server's own network and the
  cloud metadata endpoint from model-chosen URLs; 30 s per page; social networks never fetched.
- **Research data:** an email is stored only when it passes the consent check; rejected, suppressed or
  unverified addresses are not kept anywhere (`research_details` drops them, page excerpt included). Web search queries (permit
  address + project facts, public record) go to Exa through OpenRouter.

## Known gaps
- On a directory page, an address that doesn't carry the business name (allowed: personal
  addresses) can't be told apart from a neighbouring listing's by code. Such leads carry
  `needs_owner_check`; the approval step (#10) must show the flag and the excerpt to the owners.
- Behind an HTTP proxy (dev sandboxes only) the connected-address check is skipped: the proxy
  resolves names itself. The production server uses no proxy.
- Sites that hide emails with Cloudflare's anti-scraping encoding fail the consent check (decoding
  it is close to address harvesting); revisit at the legal review (step 15).
- robots.txt is not checked (a few pages per company, identifying User-Agent).
- Owner message text is logged in full (fine for Gate 1; review before real client data flows).
- WhatsApp access token lives in `.env` on the server; use a system-user token with minimal
  permissions in production.
