---
name: review-pr
description: Independent review of a pull request by a separate Opus agent in a fresh context (pr-reviewer). Use for large or risky PRs before Jeff merges, or when Jeff asks for a review. Usage /review-pr <number> [--force].
---

# Review a pull request

The author must not review their own work. This skill only **prepares** the review and
**publishes** the result; the judging is done by the `pr-reviewer` subagent (Opus, read-only),
which never sees the author's reasoning.

## 1. Is a review needed?
Get the PR (`pull_request_read` get + get_files). It is **large or risky** (CLAUDE.md §10b) if:
- more than ~300 changed lines outside `tests/` and `docs/`, **or**
- it touches `app/db/migrations/`, outbound senders or guards (`app/whatsapp/client.py`,
  Gmail sending, `app/guards/`), `app/approvals.py`, `app/graphs/`, or `.github/workflows/`.

Not large/risky and no `--force` → tell the user in one line and stop.

## 2. Gather the inputs (facts only, no opinions)
- Linked issue number (from `Closes #N` / `Part of #N`).
- The PR's "Review focus" line.
- CI status of the head commit (`get_check_runs`). If anything failed: get a summary with the
  `log-triage` subagent (Haiku) and pass it on.
- A throwaway worktree with the PR head:
  ```bash
  git fetch origin pull/<n>/head:review/pr-<n> && git fetch origin <base>
  git worktree add --force <scratchpad>/review-pr-<n> review/pr-<n>
  ```

## 3. Launch the reviewer
Agent tool, `subagent_type: "pr-reviewer"` (if that type isn't available in this session, use
`general-purpose` with `model: "opus"` and paste the body of `.claude/agents/pr-reviewer.md` as the
instructions). Run it in the foreground. Prompt contains **only**: PR number, base branch, worktree
path, issue number, CI status + log summary, Review focus line. Do **not** add your own view of
the PR, the conversation, or why things were done.

## 4. Publish
1. Post the reviewer's report **unchanged** as one PR comment (`add_issue_comment` on the PR),
   followed by the attribution footer the session requires. Don't soften or filter findings; if
   you think one is wrong, say so in chat, not in the comment.
2. In chat: the verdict, the blocking items in one line each, and the comment link.
3. Clean up: `git worktree remove --force <path>` and `git branch -D review/pr-<n>`.

## 5. After the review
- Blocking findings → fixed through `/work-on-issue` (by the author), then `/review-pr` again if
  the fix was substantial.
- Should-fix / nits → fix, or reply on the PR why not.
- Disagree with a finding → explain in chat; Jeff decides.
- **Never merge or deploy** — that stays with Jeff (CLAUDE.md §10b).
