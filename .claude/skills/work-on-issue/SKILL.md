---
name: work-on-issue
description: Run a GitHub issue end to end in this repo, keeping the issue updated in real time — start (branch, plan, draft PR), progress comments, blocked handling, and finish. Use when starting or resuming work on an issue number.
---

# Work on an issue

The issue is the single place where Jeff sees progress. Keep it true at all times.

## 1. Start
1. Read the issue, its "Done when" list, and CLAUDE.md rules that apply.
2. Sync and branch: `git fetch origin && git checkout -b <type>/<N>-<short-desc> origin/main`.
3. Comment on the issue — **Plan**: 3–5 bullets, and anything you need from Jeff/owners.
4. First commit as soon as there is something real, push, and open a **draft PR** with
   `/pull-request` (body starts `Closes #N`). The issue now shows a linked PR.

## 2. Progress
Comment on the issue at milestones only (a part works end to end, a design choice was made,
scope changed). Format: `**Progress** — <what now works>. Next: <next step>.`
Tick "Done when" boxes in the issue body as they are met.
The issue-sync workflow posts PR state changes automatically; don't duplicate those.

## 3. Blocked
As soon as you can't continue without someone:
- Comment `**Blocked** — <what>. Need from <who>: <exactly what>. Recommendation: <answer>.`
- Add `needs decision` (a choice) or `question` (missing info).
- Tell the user in chat too. Continue on any part that isn't blocked.
When unblocked: remove the label and comment what was decided.

## 4. Finish
1. Checks pass, docs updated (`/update-docs`), all "Done when" boxes ticked or explained.
2. Refresh the PR with `/pull-request`, mark it ready for review.
3. After merge, the issue closes itself. If anything was left out, open follow-up issues and
   link them in a final comment: `**Done** — <one line>. Follow-ups: #A, #B.`

## Don'ts
- Don't comment on every commit or narrate.
- Don't put secrets or personal data (phones, emails, prospect info) in issues or PRs.
- Don't close issues by hand unless the work is abandoned (then `wontfix` + reason).
