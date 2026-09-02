---
name: babysit-prs
description: Triggers when the user says "babysit my PRs", "watch my open PRs", "check on my PRs", "which of my PRs need attention", "re-run my failed CI", "are any of my PRs ready to merge", or any similar request to shepherd their own open pull requests. Runs the babysit-prs tool, which scans the user's recently-active open PRs (optionally limited to specific owners), re-runs failed required checks, and sends a macOS notification when a PR needs a human. Safe to re-run; per-PR state prevents duplicate actions and notifications.
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
**required** checks once per head commit and sends one macOS notification per PR
when a human is needed: merge conflicts, changes requested, a new review
comment (including the Copilot reviewer's; noisy bots like CI and Dependabot
excluded), a required check still failing after the retry, a non-draft PR that
is green and ready to merge, or an authored PR that
has sat ready with no activity for `--nudge-weekdays` weekdays (default 3) and
needs a reviewer nudge. The `--active-days` scan window is widened
automatically to cover the nudge threshold, so a short window never silently
hides a nudge-eligible PR.

Guardrails: check re-runs apply only to PRs the user authored; PRs they are
merely assigned to are alert-only. Only required checks are re-run. Branch
updates and merges from the base remain manual so required approvals are not
dismissed. Anything ambiguous is left for the user.

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

1. Read the printed summary (`scanned=N reran=N notified=N`).
2. Each flagged PR is logged as an `attention owner/repo#N: <reasons> (<url>)`
   line; read those to tell the user which PRs need attention and why.
3. If `ERROR:` lines appear on stderr, surface them (most commonly an expired
   `gh auth` token).

## What this skill must NOT do

- Do not re-run or touch checks that are not in the base branch's **required**
  set. When the required set is unknown, do nothing on CI.
- Never update a PR branch or merge its base branch automatically.
- Do not act on PRs the user did not author. Auto-actions are author-only; PRs
  they are merely assigned to are alert-only.
- Never deploy PRs to a review environment. Deployment remains a manual action.
- The Copilot reviewer's comments DO trigger a notification (they are
  actionable), but do not draft replies to them expecting a response; the
  Copilot reviewer cannot respond. Act on the feedback directly instead.
