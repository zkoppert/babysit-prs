---
name: babysit-prs
description: Triggers when the user says "babysit my PRs", "watch my open PRs", "check on my PRs", "which of my PRs need attention", "re-run my failed CI", "are any of my PRs ready to merge", or any similar request to shepherd their own open pull requests. Runs the babysit-prs tool, which scans the user's recently-active open PRs (optionally limited to specific owners), re-runs failed required checks, updates cleanly-behind branches when the base requires up-to-date branches, and sends a macOS notification when a PR needs a human (merge conflicts, changes requested, a new human review comment, a failed branch update, or a green ready-to-merge PR). Safe to re-run; a per-PR state signature means the user is pinged only when a PR's state actually changes.
---

# Babysit my open PRs

## When to use this skill

Use whenever the user asks any of:

- "babysit my PRs"
- "watch my open PRs"
- "which of my PRs need attention?"
- "re-run my failed CI"
- "are any of my PRs ready to merge?"

## What it does

For each of the user's recently-active open PRs (the union of PRs they
authored and PRs assigned to them, bounded by `--active-days`, default 14;
optionally limited to specific owners via `--owner`), it re-runs failed
**required** checks once per head commit, updates the branch when the base is
strict and the PR is cleanly behind, and sends one macOS notification per PR
when a human is needed: merge conflicts, changes requested, a new review
comment (including the Copilot reviewer's; noisy bots like CI and Dependabot
excluded), a failed branch update, a required check still failing after the
retry, a non-draft PR that is green and ready to merge, or an authored PR that
has sat ready with no activity for `--nudge-weekdays` weekdays (default 3) and
needs a reviewer nudge. The `--active-days` scan window is widened
automatically to cover the nudge threshold, so a short window never silently
hides a nudge-eligible PR.

Guardrails: auto-actions (re-running checks, updating the branch) apply only to
PRs the user authored; PRs they are merely assigned to are alert-only. Only
required checks are re-run, and a branch is only updated when the base is strict
and cleanly behind. Anything ambiguous is left for the user.

## How to run

```bash
# Preview without acting or notifying.
python3 babysit_prs.py --dry-run --verbose

# Default run.
python3 babysit_prs.py

# Limited to specific owners.
python3 babysit_prs.py --owner my-org --owner my-other-org

# Change the reviewer-nudge threshold (0 disables it).
python3 babysit_prs.py --nudge-weekdays 5
```

## After running

1. Read the printed summary (`scanned=N reran=N updated=N notified=N`).
2. Each flagged PR is logged as an `attention owner/repo#N: <reasons> (<url>)`
   line; read those to tell the user which PRs need attention and why.
3. If `ERROR:` lines appear on stderr, surface them (most commonly an expired
   `gh auth` token).

## What this skill must NOT do

- Do not re-run or touch checks that are not in the base branch's **required**
  set. When the required set is unknown, do nothing on CI.
- Do not update a branch unless the base requires up-to-date branches and the
  branch is cleanly behind. Never auto-resolve merge conflicts.
- Do not act on PRs the user did not author. Auto-actions are author-only; PRs
  they are merely assigned to are alert-only.
- The Copilot reviewer's comments DO trigger a notification (they are
  actionable), but do not draft replies to them expecting a response; the
  Copilot reviewer cannot respond. Act on the feedback directly instead.
