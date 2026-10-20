---
name: pull-request
description: Write or update a pull request in this repo's fixed format (short, plain, linked to its issue). Use whenever opening a PR, marking one ready for review, or refreshing its description after new commits.
---

# Pull request

## Before writing
1. Find the issue: from the branch name (`<type>/<issue#>-...`). No issue → create one first
   (rules in CLAUDE.md §10b). Read its "Done when" list.
2. Read the real change: `git log origin/main..HEAD --oneline` and `git diff origin/main...HEAD --stat`,
   then the diff itself. Describe what the code does now, not what you planned.
3. Run `/update-docs` if behaviour changed, and the checks (`uv run ruff check .`,
   `uv run ruff format --check .`, `uv run pytest`).

## Title
`<Imperative summary, ≤ 60 chars> (#<issue>)` — e.g. `Add leads, touches and suppression tables (#6)`.
No type prefixes, no trailing period.

## Body — exactly these sections, in this order

```markdown
Closes #<issue>

## Why
One or two sentences: the problem or goal, in plain words.

## What changed
- 3–6 bullets, one change each, most important first.
- Name files only when it helps a reviewer find things (`app/worker.py`).

## How to verify
- What was run and its result (tests, lint, manual steps).
- Steps a reviewer can repeat, if any.

## Notes
Only if needed: follow-ups (as issue links), risks, decisions needed, things deliberately left out.
Delete this section when empty.
```

## Rules
- Lean: the whole body should fit on one screen. No restating the diff line by line.
- Plain language a non-specialist can follow; no internal jargon, no hype words.
- Partial work: say so under **Notes** and keep the PR in draft; use `Part of #N` instead of
  `Closes #N` if the issue won't be finished by this PR.
- Never include secrets, tokens, phone numbers, emails or prospect data.
- Draft while work is ongoing; mark ready for review only when checks pass and "Done when" is met.
- After new commits, update **What changed** and **How to verify** so they stay true.
- End the body with the attribution footer the session requires, if any.
