---
name: work-on-issue
description: Run a GitHub issue end to end in this repo, keeping the issue updated in real time — start (branch, plan, decision checkpoint, draft PR), progress, blocked handling, independent review for large PRs, and finish. Use when starting or resuming work on an issue number.
---

# Work on an issue

The issue is the single place where Jeff sees progress. Keep it true at all times.

## 1. Start
1. Read the issue, its "Done when" list, and CLAUDE.md rules that apply.
2. Sync and branch: `git fetch origin && git checkout -b <type>/<N>-<short-desc> origin/main`.
3. Comment on the issue — **Plan**: 3–5 bullets, and anything you need from Jeff/owners.
   Large, compliance-sensitive or unclear design (schema, outbound/approval flows, new
   integration)? Ask the `architect` subagent (Opus) for a plan first.
4. **Decision checkpoint.** If the plan contains anything unclear or a product decision
   (business rule, threshold, owner/client-facing wording, splitting the issue), **ask Jeff in
   chat** with a recommended answer and wait. Unattended: comment it on the issue, add
   `needs decision`, and work only on the parts that don't depend on it.
5. First commit as soon as there is something real, push, and open a **draft PR** with
   `/pull-request` (body starts `Closes #N`). The issue now shows a linked PR.

## 2. Build
- Tests for every new behaviour. Run **new or changed tests several times**
  (e.g. `for i in $(seq 5); do uv run pytest -q tests/test_x.py; done`); any failure = flaky,
  fix before going further. Never assume an order between messages from different owners.
- **Bug fixes:** after the fix, search the codebase and tests for the **same pattern** and fix or
  list every other instance. Say where you looked in the PR.
- CI failed? Get the facts with the `log-triage` subagent (Haiku), reproduce locally, compare with
  `main`. Cause still unclear after one honest attempt → `architect` subagent (Opus).
- Something surprised you (wrong assumption, tricky platform behaviour)? Add one line to
  CLAUDE.md **Lessons** in the same PR.

## 3. Progress
Comment on the issue at milestones only (a part works end to end, a design choice was made,
scope changed). Format: `**Progress** — <what now works>. Next: <next step>.`
Tick "Done when" boxes in the issue body as they are met.
The issue-sync workflow posts PR state changes automatically; don't duplicate those.

## 4. Blocked
As soon as you can't continue without someone:
- Comment `**Blocked** — <what>. Need from <who>: <exactly what>. Recommendation: <answer>.`
- Add `needs decision` (a choice) or `question` (missing info).
- Tell the user in chat too. Continue on any part that isn't blocked.
When unblocked: remove the label and comment what was decided.

## 5. Finish
1. Checks pass, docs updated (`/update-docs`), all "Done when" boxes ticked or explained.
2. Refresh the PR with `/pull-request`, mark it ready for review.
3. **Large or risky PR** (CLAUDE.md §10b: >300 non-test lines, or migrations, outbound senders/
   guards, approvals/graphs, `.github/workflows/`) → run `/review-pr <n>` (independent Opus
   reviewer). Fix every blocking finding, re-run checks; for "should fix"/nits, fix or reply why not.
4. Tell Jeff it's ready: PR link, verdict, anything he should look at. **Never merge or deploy**
   unless Jeff explicitly asks for that PR in the conversation.
5. After merge, the issue closes itself. If anything was left out, open follow-up issues and
   link them in a final comment: `**Done** — <one line>. Follow-ups: #A, #B.`

## Don'ts
- Don't comment on every commit or narrate.
- Don't put secrets or personal data (phones, emails, prospect info) in issues or PRs.
- Don't close issues by hand unless the work is abandoned (then `wontfix` + reason).
