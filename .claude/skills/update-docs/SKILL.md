---
name: update-docs
description: Update the technical docs in docs/ to match code changes on the current branch. Use after changing models, migrations, routes, workflows, WhatsApp code, config or guards, and before opening or updating a PR.
---

# Update technical docs

1. Find what changed on this branch: `git diff --name-only origin/main...HEAD` plus uncommitted
   changes (`git status --short`).
2. Read `docs/doc-map.toml` to see which doc covers each changed file. Also update a doc when a
   change affects it even if the file isn't mapped (e.g. a new module → `docs/architecture.md`
   code layout table; a new env var → `docs/configuration.md`; a new graph → `docs/workflows.md`).
   A new area with no doc yet → create one, add it to `docs/README.md` and `docs/doc-map.toml`.
3. For each affected doc, read the changed code and the doc, then edit the doc so it describes
   the code **as it is now**: tables/columns/statuses, routes, flows, keys, env vars, rule status
   in `security-and-compliance.md`. Remove what no longer exists. Move items from "Planned" to
   the main sections when they land.
4. Keep the style: short, tables and diagrams over prose, file paths for every claim, no history
   ("we changed X") — git has the history.
5. If `CLAUDE.md` (§5 layout, §6 data model, §9 status, §12 decisions) or `RUNBOOK.md` (anything
   operators run) is now wrong, fix it too.
6. Run `uv run python -m scripts.check_docs` and commit the docs with the code
   (message like `docs: update data model for leads table`).
