---
name: new-issue
description: Create a GitHub issue in this repo in the fixed format (Goal, Done when, Depends on, Notes, Size) after checking for duplicates. Use whenever a new task, bug, chore or follow-up needs tracking.
---

# New issue

## 1. Check before writing
1. Search open **and closed** issues for the same work (title keywords, the file or feature).
   - Open match → comment there instead of creating a new one.
   - Closed match that regressed → reopen it with a comment explaining what came back.
2. If the requirement is unclear or contains a product decision (business rule, threshold,
   wording owners/clients see, scope), **ask Jeff in chat first** with a recommended answer.
   Write the issue only once it's settled; record the decision under **Notes** with the date.

## 2. Write it

**Title:** what will be true when done, ≤ 70 chars, no type prefix
(`Ingest Montréal building permits daily`, not `feat: permits`).
Bugs: describe the symptom (`Owner gets the same alert twice after a restart`).

**Body — these sections, in order:**

```markdown
## Goal
One or two sentences: what changes for Jeff, the owners or the system, and why.

## Done when
- [ ] Concrete, checkable results (a command, a visible behaviour, a test)
- [ ] `docs/` updated

## Depends on
#N, #M — or "nothing". 

## Notes
Relevant CLAUDE.md rules (§2 numbers), decisions taken (with date), links. Delete if empty.

## Size
S (< ½ day) · M (≤ 2 days) · L (more)
```

Bugs also include, before **Done when**: **What happens** (steps, expected vs actual, where/when)
and, if known, **Cause**.

**Labels — only these:** `enhancement` (features, build steps, infra), `bug`, `documentation`,
plus `needs decision` (waiting on a choice) or `question` (missing information) when blocked.
Automated runtime failures use `bug` + title prefix `[agent]` (created by code, not by this skill).

## 3. Size and splits
An **L** that contains parts which could ship and be verified separately: propose the split
**in chat** (titles + one line each) and wait for Jeff's answer before creating several issues.

## Rules
- One issue = one outcome. "And" in the title is a hint it should be two.
- No secrets, phone numbers, emails or prospect data in issues.
- End the body with the attribution footer the session requires, if any.
- New issues land on the board automatically (Backlog).
