#!/usr/bin/env python3
"""Babysit your own open pull requests so you stop hand-watching CI.

For each of your open PRs (optionally limited to one or more owners), this
tool:

- Re-runs failed **required** checks once per head commit (a flaky-test
  retry), and only notifies you if they are still red after that retry.
- Updates the branch when the base requires up-to-date branches and the
  PR is cleanly behind (``mergeStateStatus == BEHIND``), so CI re-runs
  against the latest base without your involvement.
- Notifies you on macOS when something needs a human: merge conflicts,
  changes requested, a new human review comment, a failed branch update,
  or a PR that is green and ready to merge.

Design goals:

- Auto-act with guardrails: only touch **required** checks and only
  update-branch when the base is strict and the branch is cleanly behind.
  Anything ambiguous (unknown required set, conflicts) is left for you.
- Quiet by default: a per-PR state signature means you are pinged only
  when a PR's notable state actually changes, not every run.
- Dry-run friendly: ``--dry-run`` skips every mutation and notification.
- Pure stdlib: no third-party runtime dependencies.

Requires the ``gh`` CLI authenticated with ``repo`` and ``workflow``
scopes. macOS is required for notifications (``terminal-notifier`` makes
them clickable; otherwise it falls back to ``osascript``).

Usage:
    babysit_prs.py [--owner OWNER] [--active-days N] [--allowed-repo OWNER/REPO]
                   [--skip-repo OWNER/REPO] [--dry-run] [--no-notify]
                   [--state-file PATH] [--verbose]
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("babysit-prs")

DEFAULT_STATE_FILE = Path.home() / "Library" / "Logs" / "babysit-prs-state.json"

# statusCheckRollup CheckRun conclusions that count as a failure worth
# re-running.
FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
# StatusContext states that count as a failure.
FAILURE_STATES: frozenset[str] = frozenset({"FAILURE", "ERROR"})
# CheckRun statuses that mean the check has not finished yet.
PENDING_STATUSES: frozenset[str] = frozenset(
    {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}
)

# Alert tokens.
ALERT_CONFLICTS = "conflicts"
ALERT_CHANGES_REQUESTED = "changes-requested"
ALERT_CI_STILL_FAILING = "ci-failing"
ALERT_UPDATE_FAILED = "update-failed"
ALERT_NEW_COMMENT = "new-comment"
ALERT_READY = "ready-to-merge"
ALERT_NUDGE_REVIEWERS = "nudge-reviewers"

# Default number of weekdays a ready PR may sit with no updates, reviews, or
# comments before the tool suggests nudging the reviewers.
DEFAULT_NUDGE_WEEKDAYS = 3

# Severity order for the notification summary line.
ALERT_ORDER: tuple[str, ...] = (
    ALERT_CONFLICTS,
    ALERT_CHANGES_REQUESTED,
    ALERT_CI_STILL_FAILING,
    ALERT_UPDATE_FAILED,
    ALERT_NEW_COMMENT,
    ALERT_NUDGE_REVIEWERS,
    ALERT_READY,
)
ALERT_LABELS: dict[str, str] = {
    ALERT_CONFLICTS: "Merge conflicts",
    ALERT_CHANGES_REQUESTED: "Changes requested",
    ALERT_CI_STILL_FAILING: "Failing CI",
    ALERT_UPDATE_FAILED: "Branch update failed",
    ALERT_NEW_COMMENT: "New review comment",
    ALERT_NUDGE_REVIEWERS: "Waiting on reviewers, time to nudge",
    ALERT_READY: "Ready to merge",
}


# ---------------------------------------------------------------------------
# gh helpers
# ---------------------------------------------------------------------------


def run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Run ``gh <args>`` and return stdout, or raise on non-zero exit."""
    cmd = ["gh", *args]
    logger.debug("running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout


def get_my_login() -> str:
    """Return the authenticated GitHub login from ``gh api /user``."""
    out = run_gh(["api", "/user"])
    payload = json.loads(out)
    if not isinstance(payload, dict) or not payload.get("login"):
        raise LookupError(f"unexpected /user response shape: {type(payload).__name__}")
    login = payload["login"]
    if not isinstance(login, str):
        raise LookupError(f"login field is not a string: {type(login).__name__}")
    return login


def search_my_open_prs(
    owners: set[str] | None,
    allowed: set[str],
    active_days: int = 14,
    skip: set[str] | None = None,
) -> list[tuple[str, int]]:
    """Return ``(repo, number)`` for your recently-active open PRs.

    The result is the union of PRs you authored and PRs assigned to you,
    deduplicated. ``owners`` optionally limits the scan to one or more
    org/user owners; when empty or None, all such PRs are considered.
    ``active_days`` bounds the scan to PRs updated within that many days so
    the loop stays fast even when hundreds of stale PRs are open. ``skip``
    drops specific repos (for example a fixture repo whose state must not be
    mutated) even though the owner is in scope.
    """
    owners = owners or set()
    skip = skip or set()
    prs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    # Author and assignee are separate GitHub search qualifiers; combining
    # them in one query would AND (PRs both authored and assigned), so run
    # each role separately and union the results.
    for role in ("--author=@me", "--assignee=@me"):
        for repo, number in _search_role(role, owners, active_days):
            key = (repo, number)
            if key in seen or repo in skip:
                continue
            if allowed and repo not in allowed:
                continue
            seen.add(key)
            prs.append(key)
    return prs


def _search_role(
    role_flag: str, owners: set[str], active_days: int
) -> list[tuple[str, int]]:
    """Run one ``gh search prs`` role query and parse ``(repo, number)`` rows."""
    args = [
        "search",
        "prs",
        role_flag,
        "--state=open",
        "--json",
        "number,repository",
        "--limit",
        "1000",
    ]
    if active_days > 0:
        cutoff = datetime.date.today() - datetime.timedelta(days=active_days)
        args.append(f"updated:>={cutoff.isoformat()}")
    for owner in sorted(owners):
        args.append(f"--owner={owner}")
    try:
        out = run_gh(args, timeout=45)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("search (%s) failed: %s", role_flag, exc)
        return []
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        logger.warning("search (%s) parse error: %s", role_flag, exc)
        return []
    parsed: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        repo = ((row.get("repository") or {}).get("nameWithOwner") or "").strip()
        number = row.get("number")
        if repo and isinstance(number, int):
            parsed.append((repo, number))
    return parsed


PR_FIELDS = (
    "number,title,url,state,isDraft,mergeable,mergeStateStatus,"
    "headRefOid,baseRefName,reviewDecision,statusCheckRollup,"
    "latestReviews,comments,author,updatedAt"
)


def fetch_pr(repo: str, number: int) -> dict[str, Any] | None:
    """Fetch the PR fields needed for the decision tree."""
    try:
        out = run_gh(
            ["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS],
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("fetch_pr failed for %s#%d: %s", repo, number, exc)
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        logger.warning("fetch_pr: parse error for %s#%d: %s", repo, number, exc)
        return None


def fetch_review_comments(repo: str, number: int) -> list[dict[str, Any]]:
    """Return inline review-thread comments for a PR.

    ``gh pr view`` exposes submitted reviews and conversation comments but
    not inline review-thread replies, so fetch those from the REST API and
    normalize each to the ``{author, createdAt}`` shape the activity scan
    already understands. Errors return an empty list (a missed inline reply
    this run, never a crash).
    """
    try:
        out = run_gh(
            [
                "api",
                f"/repos/{repo}/pulls/{number}/comments",
                "--paginate",
                "--slurp",
            ],
            timeout=30,
        )
        pages = json.loads(out or "[]")
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning("fetch_review_comments failed for %s#%d: %s", repo, number, exc)
        return []
    comments: list[dict[str, Any]] = []
    for page in pages if isinstance(pages, list) else []:
        for item in page if isinstance(page, list) else [page]:
            if not isinstance(item, dict):
                continue
            login = (item.get("user") or {}).get("login") or ""
            when = item.get("created_at") or ""
            if login and when:
                comments.append({"author": {"login": login}, "createdAt": when})
    return comments


@dataclass
class RequiredChecks:
    """Required status checks for a base branch.

    ``contexts`` is ``None`` when the required set could not be read (no
    admin, ruleset not exposed), which callers treat as "unknown" and
    skip CI auto-actions on.
    """

    contexts: set[str] | None
    strict: bool

    @property
    def known(self) -> bool:
        """Return True when the required-check set could be read."""
        return self.contexts is not None


def fetch_required_checks(repo: str, base: str) -> RequiredChecks:
    """Read required status checks + strict flag for ``repo``'s base branch.

    GitHub can layer classic branch protection and organization rulesets on
    the same branch, so both sources are queried and their requirements are
    unioned (strict is true if either is strict). Returns an unknown
    ``RequiredChecks`` only when neither source is readable.
    """
    if not base:
        return RequiredChecks(contexts=None, strict=False)
    protection = _fetch_required_checks_from_protection(repo, base)
    rules = _fetch_required_checks_from_rules(repo, base)
    if not protection.known and not rules.known:
        return RequiredChecks(contexts=None, strict=False)
    contexts: set[str] = set()
    if protection.contexts:
        contexts |= protection.contexts
    if rules.contexts:
        contexts |= rules.contexts
    strict = bool(protection.strict or rules.strict)
    return RequiredChecks(contexts=contexts, strict=strict)


def _fetch_required_checks_from_protection(repo: str, base: str) -> RequiredChecks:
    try:
        out = run_gh(
            [
                "api",
                f"/repos/{repo}/branches/{base}/protection/required_status_checks",
            ],
            timeout=20,
        )
        data = json.loads(out)
    except subprocess.CalledProcessError as exc:
        logger.debug("no branch protection for %s@%s: %s", repo, base, exc)
        return RequiredChecks(contexts=None, strict=False)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "required checks (protection) error for %s@%s: %s", repo, base, exc
        )
        return RequiredChecks(contexts=None, strict=False)
    return RequiredChecks(
        contexts=_protection_contexts(data), strict=bool(data.get("strict"))
    )


def _protection_contexts(data: dict[str, Any]) -> set[str]:
    contexts: set[str] = set()
    for ctx in data.get("contexts") or []:
        if isinstance(ctx, str) and ctx:
            contexts.add(ctx)
    for check in data.get("checks") or []:
        if isinstance(check, dict):
            ctx = check.get("context")
            if isinstance(ctx, str) and ctx:
                contexts.add(ctx)
    return contexts


def _fetch_required_checks_from_rules(repo: str, base: str) -> RequiredChecks:
    try:
        out = run_gh(["api", f"/repos/{repo}/rules/branches/{base}"], timeout=20)
        rules = json.loads(out)
    except subprocess.CalledProcessError as exc:
        logger.debug("no rulesets for %s@%s: %s", repo, base, exc)
        return RequiredChecks(contexts=None, strict=False)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("required checks (rules) error for %s@%s: %s", repo, base, exc)
        return RequiredChecks(contexts=None, strict=False)

    contexts: set[str] = set()
    strict = False
    found = False
    if isinstance(rules, list):
        for rule in rules:
            if (
                not isinstance(rule, dict)
                or rule.get("type") != "required_status_checks"
            ):
                continue
            found = True
            params = rule.get("parameters") or {}
            strict = strict or bool(params.get("strict_required_status_checks_policy"))
            for check in params.get("required_status_checks") or []:
                if isinstance(check, dict):
                    ctx = check.get("context")
                    if isinstance(ctx, str) and ctx:
                        contexts.add(ctx)
    if not found:
        return RequiredChecks(contexts=None, strict=False)
    return RequiredChecks(contexts=contexts, strict=strict)


# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------


def _is_bot(login: str) -> bool:
    """Return True for bot accounts (including the Copilot reviewer)."""
    lowered = login.lower()
    return lowered.endswith("[bot]") or lowered in {"copilot", "github-actions"}


def _is_copilot_reviewer(login: str) -> bool:
    """Return True for the Copilot code-review bot.

    Its review comments are actionable feedback, so they count as
    notify-worthy activity even though it is technically a bot.
    """
    return "copilot" in login.lower()


def check_name(entry: dict[str, Any]) -> str:
    """Return the check name (CheckRun) or context (StatusContext)."""
    return (entry.get("name") or entry.get("context") or "").strip()


def check_is_pending(entry: dict[str, Any]) -> bool:
    """Return True when a rollup check has not finished yet."""
    if entry.get("__typename") == "CheckRun":
        return (entry.get("status") or "").upper() in PENDING_STATUSES
    return (entry.get("state") or "").upper() in {"PENDING", "EXPECTED"}


def check_is_failed(entry: dict[str, Any]) -> bool:
    """Return True when a completed rollup check counts as failed."""
    if entry.get("__typename") == "CheckRun":
        if (entry.get("status") or "").upper() in PENDING_STATUSES:
            return False
        return (entry.get("conclusion") or "").upper() in FAILURE_CONCLUSIONS
    return (entry.get("state") or "").upper() in FAILURE_STATES


def _has_unfinished_or_failing_check(pr: dict[str, Any]) -> bool:
    """Return True when any visible rollup check is pending or has failed.

    Unlike ``required_check_status`` this ignores the required-check set, so it
    still sees red or in-flight CI when branch protection is unreadable. Used
    only to suppress the reviewer nudge in that blind spot, where we cannot
    tell whether an unfinished or failed check is the real blocker.
    """
    return any(
        isinstance(entry, dict) and (check_is_pending(entry) or check_is_failed(entry))
        for entry in pr.get("statusCheckRollup") or []
    )


def required_check_status(
    pr: dict[str, Any], required: RequiredChecks
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(failed_required_entries, any_required_pending)``.

    Returns the failed required rollup entries (not just names) so the
    caller can resolve each to its Actions run. When the required set is
    unknown, returns no failures and no pending so callers skip CI
    handling entirely.
    """
    if not required.known:
        return [], False
    contexts = required.contexts or set()
    failed: list[dict[str, Any]] = []
    pending = False
    for entry in pr.get("statusCheckRollup") or []:
        if not isinstance(entry, dict):
            continue
        if check_name(entry) not in contexts:
            continue
        if check_is_pending(entry):
            pending = True
        elif check_is_failed(entry):
            failed.append(entry)
    return failed, pending


def _run_id_from_details_url(url: str) -> int | None:
    """Extract the Actions run id from a CheckRun ``detailsUrl``."""
    match = re.search(r"/actions/runs/(\d+)", url or "")
    return int(match.group(1)) if match else None


def rerunnable_run_ids(entries: list[dict[str, Any]]) -> list[int]:
    """Return the distinct Actions run ids backing failed CheckRun entries.

    External ``StatusContext`` checks (and CheckRuns whose detailsUrl has
    no run id) are skipped: they are not Actions runs and cannot be
    re-run, so the caller surfaces them for a human instead.
    """
    ids: list[int] = []
    for entry in entries:
        if entry.get("__typename") != "CheckRun":
            continue
        run_id = _run_id_from_details_url(entry.get("detailsUrl") or "")
        if run_id is not None and run_id not in ids:
            ids.append(run_id)
    return ids


def latest_human_activity(pr: dict[str, Any], my_login: str) -> str | None:
    """Return the newest ISO timestamp of notify-worthy review activity.

    Covers submitted reviews (``latestReviews``), conversation comments
    (``comments``), and inline review-thread replies (``reviewComments``,
    populated by the caller). Excludes your own activity and noisy bots (CI,
    Dependabot), but the Copilot code reviewer counts, since its comments are
    actionable feedback worth a notification.
    """
    stamps: list[str] = []
    for review in pr.get("latestReviews") or []:
        if isinstance(review, dict):
            when = _human_stamp(review, my_login, "submittedAt")
            if when:
                stamps.append(when)
    for comment in (pr.get("comments") or []) + (pr.get("reviewComments") or []):
        if isinstance(comment, dict):
            when = _human_stamp(comment, my_login, "createdAt")
            if when:
                stamps.append(when)
    return max(stamps) if stamps else None


def _human_stamp(item: dict[str, Any], my_login: str, key: str) -> str | None:
    login = (item.get("author") or {}).get("login") or ""
    if not login or login == my_login:
        return None
    # Exclude noisy bots, but keep the Copilot reviewer (actionable feedback).
    if _is_bot(login) and not _is_copilot_reviewer(login):
        return None
    when = item.get(key)
    return when if isinstance(when, str) and when else None


def _parse_iso(ts: str) -> datetime.datetime | None:
    """Parse a GitHub ISO-8601 timestamp (``...Z``) to an aware datetime."""
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def weekdays_since(ts: str, now: datetime.datetime) -> int:
    """Count weekday (Mon-Fri) dates elapsed since ``ts`` up to ``now``.

    Works at date granularity in ``now``'s timezone (production passes a
    local-aware ``now``; tests pass UTC for determinism). Weekends are not
    counted; public holidays are not modeled. Returns 0 when ``ts`` is
    unparseable or in the future, so a missing timestamp never nudges.
    """
    start = _parse_iso(ts)
    if start is None:
        return 0
    tz = now.tzinfo or datetime.timezone.utc
    start_date = start.astimezone(tz).date()
    end_date = now.astimezone(tz).date()
    count = 0
    day = start_date + datetime.timedelta(days=1)
    while day <= end_date:
        if day.weekday() < 5:
            count += 1
        day += datetime.timedelta(days=1)
    return count


@dataclass
class Decision:
    """Result of classifying one PR (pure; no state transitions here)."""

    alerts: list[str] = field(default_factory=list)
    do_rerun: bool = False
    rerun_run_ids: list[int] = field(default_factory=list)
    do_update_branch: bool = False
    current_activity: str | None = None


def classify(
    pr: dict[str, Any],
    required: RequiredChecks,
    my_login: str,
    prior: dict[str, Any],
    now: datetime.datetime | None = None,
    nudge_weekdays: int = DEFAULT_NUDGE_WEEKDAYS,
) -> Decision:
    """Decide alerts and auto-actions for one PR (pure, no side effects).

    Auto-actions (re-running checks, updating the branch) apply only to PRs
    you authored; PRs you are merely assigned to are alert-only. State
    transitions (advancing ``rerun_head`` / ``update_head`` /
    ``last_activity`` / ``notified_sig``) are the caller's job and depend on
    whether the actions and the notification actually succeed.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    decision = Decision()
    authored = ((pr.get("author") or {}).get("login") or "") == my_login
    required_pending = _classify_actions(pr, required, prior, authored, decision)
    _classify_alerts(pr, my_login, prior, decision)
    if not _nudge_blocked(pr, required, decision, required_pending):
        _classify_nudge(pr, now, nudge_weekdays, authored, decision)
    return decision


def _classify_actions(
    pr: dict[str, Any],
    required: RequiredChecks,
    prior: dict[str, Any],
    authored: bool,
    decision: Decision,
) -> bool:
    """Set auto-actions (update branch, re-run CI). Returns required_pending.

    A cleanly-behind branch on a strict base is refreshed first; that creates
    a new head and re-runs CI, so old-head CI work is skipped for that cycle.
    Both auto-actions are gated on authorship (only touch my own branches).
    """
    head = pr.get("headRefOid") or ""
    mss = (pr.get("mergeStateStatus") or "").upper()
    failed_entries, required_pending = required_check_status(pr, required)
    behind = mss == "BEHIND" and required.known and required.strict
    if authored and behind and prior.get("update_head", "") != head:
        decision.do_update_branch = True
    # Only skip CI classification when we are actually updating the branch; an
    # assigned (or already-updated) behind PR still surfaces failing CI.
    if not decision.do_update_branch:
        _classify_ci(failed_entries, required_pending, head, prior, decision, authored)
    return required_pending


def _classify_alerts(
    pr: dict[str, Any],
    my_login: str,
    prior: dict[str, Any],
    decision: Decision,
) -> None:
    """Append the human-needed alerts: conflicts, changes, new comment, ready."""
    mss = (pr.get("mergeStateStatus") or "").upper()
    if mss == "DIRTY" or (pr.get("mergeable") or "").upper() == "CONFLICTING":
        decision.alerts.append(ALERT_CONFLICTS)
    if (pr.get("reviewDecision") or "").upper() == "CHANGES_REQUESTED":
        decision.alerts.append(ALERT_CHANGES_REQUESTED)
    activity = latest_human_activity(pr, my_login)
    decision.current_activity = activity
    if activity and activity != prior.get("last_activity", ""):
        decision.alerts.append(ALERT_NEW_COMMENT)
    if not bool(pr.get("isDraft")) and mss == "CLEAN":
        decision.alerts.append(ALERT_READY)


def _nudge_blocked(
    pr: dict[str, Any],
    required: RequiredChecks,
    decision: Decision,
    required_pending: bool,
) -> bool:
    """Return True when the reviewers are not the blocker, so no nudge.

    Blocked when an auto-action was just scheduled, CI is pending or failing,
    the PR is conflicted or has requested changes, it is already approved (then
    it is mine to merge), or it is ready to merge. When the required-check set
    is unreadable we cannot tell whether a red check is blocking, so we fall
    back to the raw rollup and keep the ball on the author rather than nudging
    reviewers about a PR that may be failing.
    """
    if decision.do_rerun or decision.do_update_branch or required_pending:
        return True
    if not required.known and _has_unfinished_or_failing_check(pr):
        return True
    if (pr.get("reviewDecision") or "").upper() == "APPROVED":
        return True
    blocking = {
        ALERT_CONFLICTS,
        ALERT_CHANGES_REQUESTED,
        ALERT_CI_STILL_FAILING,
        ALERT_READY,
    }
    return bool(blocking.intersection(decision.alerts))


def _classify_nudge(
    pr: dict[str, Any],
    now: datetime.datetime,
    nudge_weekdays: int,
    authored: bool,
    decision: Decision,
) -> None:
    """Flag an authored, non-draft PR that has sat ready idle long enough.

    The "ball is on the reviewers" guard lives in ``_nudge_blocked``; this
    only adds the time-based condition.
    """
    if not authored or bool(pr.get("isDraft")) or nudge_weekdays <= 0:
        return
    updated = pr.get("updatedAt")
    if not isinstance(updated, str) or not updated:
        return
    if weekdays_since(updated, now) >= nudge_weekdays:
        decision.alerts.append(ALERT_NUDGE_REVIEWERS)


def _classify_ci(
    failed_entries: list[dict[str, Any]],
    required_pending: bool,
    head: str,
    prior: dict[str, Any],
    decision: Decision,
    authored: bool,
) -> None:
    """Decide the CI action/alert for a PR that is not being refreshed.

    Authored PRs get one re-run per head commit, then alert if still red.
    Assigned-but-not-authored PRs are alert-only: a failed required check is
    surfaced immediately without re-running someone else's workflow.
    """
    if not failed_entries or required_pending:
        return
    run_ids = rerunnable_run_ids(failed_entries)
    already_reran = prior.get("rerun_head", "") == head
    if not authored or already_reran or not run_ids:
        # Not my branch to re-run, or we already re-ran this head and it is
        # still red, or the failed required check is an external status we
        # cannot re-run.
        decision.alerts.append(ALERT_CI_STILL_FAILING)
    else:
        decision.do_rerun = True
        decision.rerun_run_ids = run_ids


def signature(alerts: list[str]) -> str:
    """Stable de-dup key for the persistent alerts on a PR.

    The event-like alerts ``new-comment`` and ``nudge-reviewers`` are excluded
    here and de-duped separately (by ``last_activity`` and ``nudged_at``), so
    that their firing once and then dropping off does not change the persistent
    signature and cause a spurious re-notify. Excluding the nudge also lets it
    re-fire after real activity resets the PR, rather than being a one-shot.
    """
    event_like = {ALERT_NEW_COMMENT, ALERT_NUDGE_REVIEWERS}
    return "|".join(sorted(a for a in alerts if a not in event_like))


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def rerun_runs(repo: str, run_ids: list[int], *, dry_run: bool) -> bool:
    """Re-run the failed jobs of the given Actions runs.

    Returns True when at least one re-run was triggered (or in dry-run),
    so the caller only records ``rerun_head`` when a re-run actually
    happened rather than for a re-run that never occurred.
    """
    if not run_ids:
        return False
    if dry_run:
        logger.info(
            "[dry-run] would re-run %d run(s) for %s: %s", len(run_ids), repo, run_ids
        )
        return True
    triggered = False
    for run_id in run_ids:
        try:
            run_gh(
                ["run", "rerun", str(run_id), "--repo", repo, "--failed"], timeout=30
            )
            logger.info("re-ran failed jobs in %s run %s", repo, run_id)
            triggered = True
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            logger.warning("rerun failed for %s run %s: %s", repo, run_id, exc)
    return triggered


def update_branch(repo: str, number: int, *, dry_run: bool) -> bool:
    """Update the PR branch from base. Returns True on success."""
    if dry_run:
        logger.info("[dry-run] would update branch for %s#%d", repo, number)
        return True
    try:
        run_gh(["pr", "update-branch", str(number), "--repo", repo], timeout=45)
        logger.info("updated branch for %s#%d", repo, number)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("update-branch failed for %s#%d: %s", repo, number, exc)
        return False


def _osa_str(value: str) -> str:
    """Quote a Python string as an AppleScript string literal.

    Escapes only backslash and double-quote and passes raw Unicode, since
    AppleScript string literals reject the ``\\uXXXX`` escapes that
    ``json.dumps`` would emit for accented or emoji characters.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, subtitle: str, url: str, *, dry_run: bool) -> bool:
    """Send a macOS notification for one PR. Returns True on delivery.

    Uses ``terminal-notifier`` when present so the whole notification is
    clickable and opens the PR. Otherwise falls back to ``osascript``,
    which cannot open a URL on click, so the PR url is placed in the body
    to stay copyable. The delivery result gates de-dup: a failed
    notification is not recorded, so it is retried next run.
    """
    if dry_run:
        logger.info("[dry-run] notify: %s | %s | %s", title, subtitle, url)
        return False
    tn = shutil.which("terminal-notifier")
    if tn:
        args = [tn, "-title", title, "-subtitle", subtitle, "-sound", "default"]
        args += ["-message", url, "-open", url] if url else ["-message", subtitle]
        return _run_quiet(args)
    parts = [
        f"display notification {_osa_str(url or subtitle)}",
        f"with title {_osa_str(title)}",
    ]
    if subtitle:
        parts.append(f"subtitle {_osa_str(subtitle)}")
    parts.append('sound name "default"')
    return _run_quiet(["osascript", "-e", " ".join(parts)])


def _run_quiet(args: list[str]) -> bool:
    """Run a notification command, returning True on a zero exit."""
    try:
        result = subprocess.run(args, check=False, capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("notify command failed: %s", exc)
        return False
    if result.returncode != 0:
        logger.debug("notify command exited %s: %s", result.returncode, result.stderr)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """Load the per-PR state map, returning {} when absent or unreadable."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("load_state failed for %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    """Write the state file atomically. Raises OSError on failure.

    The write goes to a temp file in the same directory and is renamed
    into place, so a crashed or overlapping run can never leave a
    half-written (unparseable) state file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".babysit-state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def acquire_lock(path: Path) -> Any:
    """Take a non-blocking exclusive lock, or return None if held.

    Overlapping runs (a slow tick, or a manual invocation on top of the
    launchd schedule) would otherwise race the read-modify-write of the
    state file. The loser simply skips this tick.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")  # pylint: disable=consider-using-with
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# ---------------------------------------------------------------------------
# Stats + run loop
# ---------------------------------------------------------------------------


@dataclass
class BabysitStats:
    """Counters returned by ``run`` so tests and logs can assert on it."""

    scanned: int = 0
    reran: int = 0
    updated: int = 0
    notified: int = 0
    errors: list[str] = field(default_factory=list)


def _alert_message(alerts: list[str]) -> str:
    ordered = [a for a in ALERT_ORDER if a in alerts]
    return ", ".join(ALERT_LABELS.get(a, a) for a in ordered)


@dataclass
class RunContext:
    """Shared state threaded through the per-PR loop."""

    my_login: str
    dry_run: bool
    no_notify: bool
    state: dict[str, dict[str, Any]]
    now: datetime.datetime
    nudge_weekdays: int = DEFAULT_NUDGE_WEEKDAYS
    required_cache: dict[str, RequiredChecks] = field(default_factory=dict)

    @property
    def suppress_notify(self) -> bool:
        """Return True when notifications should not be delivered."""
        return self.dry_run or self.no_notify

    def required_for(self, repo: str, base: str) -> RequiredChecks:
        """Return the required checks for a base branch, cached per repo."""
        key = f"{repo}@{base}"
        if key not in self.required_cache:
            self.required_cache[key] = fetch_required_checks(repo, base)
        return self.required_cache[key]


def run(args: argparse.Namespace) -> BabysitStats:
    """Main entrypoint. Returns stats so tests can assert behaviour."""
    stats = BabysitStats()
    lock = acquire_lock(args.state_file.with_suffix(".lock"))
    if lock is None:
        logger.info("another babysit run holds the lock; skipping this tick")
        return stats
    try:
        _run_locked(args, stats)
    finally:
        lock.close()
    return stats


def _run_locked(args: argparse.Namespace, stats: BabysitStats) -> None:
    try:
        my_login = get_my_login()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        LookupError,
        OSError,
    ) as exc:
        stats.errors.append(f"failed to fetch /user: {exc}")
        return

    ctx = RunContext(
        my_login=my_login,
        dry_run=args.dry_run,
        no_notify=args.no_notify,
        state=load_state(args.state_file),
        now=datetime.datetime.now().astimezone(),
        nudge_weekdays=args.nudge_weekdays,
    )
    allowed = set(args.allowed_repo)

    for repo, number in search_my_open_prs(
        set(args.owner), allowed, args.active_days, set(args.skip_repo)
    ):
        stats.scanned += 1
        try:
            pr = fetch_pr(repo, number)
            if pr is None:
                continue
            _process_pr(pr, repo, number, ctx, stats)
        except (
            subprocess.SubprocessError,
            OSError,
            json.JSONDecodeError,
            ValueError,
            LookupError,
        ) as exc:
            # One bad PR must not abort the whole launchd batch.
            logger.warning("skipping %s#%d after error: %s", repo, number, exc)
            stats.errors.append(f"{repo}#{number}: {exc}")

    if not args.dry_run:
        try:
            save_state(args.state_file, ctx.state)
        except OSError as exc:
            stats.errors.append(f"failed to save state: {exc}")


def _process_pr(
    pr: dict[str, Any],
    repo: str,
    number: int,
    ctx: RunContext,
    stats: BabysitStats,
) -> None:
    """Auto-act on one PR and notify when its state changed."""
    required = ctx.required_for(repo, pr.get("baseRefName") or "")
    pr_url = pr.get("url") or f"https://github.com/{repo}/pull/{number}"
    prior = ctx.state.get(pr_url, {})
    pr["reviewComments"] = fetch_review_comments(repo, number)
    decision = classify(pr, required, ctx.my_login, prior, ctx.now, ctx.nudge_weekdays)

    new_state = {
        "rerun_head": prior.get("rerun_head", ""),
        "update_head": prior.get("update_head", ""),
        "last_activity": prior.get("last_activity", ""),
        "notified_sig": prior.get("notified_sig", ""),
        "nudged_at": prior.get("nudged_at", ""),
    }
    head = pr.get("headRefOid") or ""

    if decision.do_rerun:
        if rerun_runs(repo, decision.rerun_run_ids, dry_run=ctx.dry_run):
            new_state["rerun_head"] = head
            stats.reran += 1
        else:
            # The re-run could not be triggered (for example missing Actions
            # permission). Record the head so we do not retry it forever, and
            # surface it as a human-needed alert this run.
            new_state["rerun_head"] = head
            decision.alerts.append(ALERT_CI_STILL_FAILING)
    if decision.do_update_branch:
        if update_branch(repo, number, dry_run=ctx.dry_run):
            new_state["update_head"] = head
            stats.updated += 1
        else:
            decision.alerts.append(ALERT_UPDATE_FAILED)

    _decide_notify(pr, pr_url, f"{repo}#{number}", decision, ctx, stats, new_state)
    ctx.state[pr_url] = new_state


def _decide_notify(
    pr: dict[str, Any],
    pr_url: str,
    title: str,
    decision: Decision,
    ctx: RunContext,
    stats: BabysitStats,
    new_state: dict[str, Any],
) -> None:
    """Notify once if the PR's state changed, and advance the de-dup state.

    The nudge is event-like: notify once per idle episode, keyed on the
    ``updatedAt`` it fired at, so a fresh idle period (new ``updatedAt``)
    re-fires but a standing nudge stays quiet every run.
    """
    sig = signature(decision.alerts)
    updated_at = pr.get("updatedAt") or ""
    nudge_event = (
        ALERT_NUDGE_REVIEWERS in decision.alerts
        and updated_at != new_state["nudged_at"]
    )
    should_notify = (
        (bool(sig) and sig != new_state["notified_sig"])
        or ALERT_NEW_COMMENT in decision.alerts
        or nudge_event
    )
    delivered = _emit(title, decision, pr_url, ctx, stats) if should_notify else False

    if not (should_notify and not delivered):
        # Nothing to notify, or notified successfully: advance de-dup.
        # Preserve a previously-notified persistent signature when the current
        # one is empty, so an alert that transiently disappears and reappears
        # on the same commit is not re-notified.
        new_state["notified_sig"] = sig or new_state["notified_sig"]
        if decision.current_activity:
            new_state["last_activity"] = decision.current_activity
        if ALERT_NUDGE_REVIEWERS in decision.alerts:
            new_state["nudged_at"] = updated_at


def _emit(
    title: str,
    decision: Decision,
    pr_url: str,
    ctx: RunContext,
    stats: BabysitStats,
) -> bool:
    """Deliver one notification. Returns True only on real delivery."""
    subtitle = _alert_message(decision.alerts)
    # Log every flagged PR so a --verbose run (and any wrapping agent) can
    # report which PRs need attention and why, not just aggregate counts.
    logger.info("attention %s: %s (%s)", title, subtitle, pr_url)
    if ctx.suppress_notify:
        if ctx.dry_run:
            notify(title, subtitle, pr_url, dry_run=True)
        return False
    delivered = notify(title, subtitle, pr_url, dry_run=False)
    if delivered:
        stats.notified += 1
    return delivered


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Babysit your own open PRs: re-run failed required checks, update "
            "cleanly-behind branches, and notify you on macOS when something "
            "needs a human."
        ),
    )
    parser.add_argument(
        "--owner",
        action="append",
        default=[],
        metavar="OWNER",
        help=(
            "Limit to PRs in this org/user owner. Pass multiple times for "
            "multiple owners. Default: all of your open PRs."
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Per-PR notify/de-dup state file (default: {DEFAULT_STATE_FILE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview decisions; do not re-run checks, update branches, or notify.",
    )
    parser.add_argument(
        "--active-days",
        type=int,
        default=14,
        metavar="N",
        help="Only watch PRs updated within the last N days (0 = no limit). Default: 14.",
    )
    parser.add_argument(
        "--nudge-weekdays",
        type=int,
        default=DEFAULT_NUDGE_WEEKDAYS,
        metavar="N",
        help=(
            "Nudge reviewers on an authored PR idle this many weekdays "
            "(0 disables). Default: 3."
        ),
    )
    parser.add_argument(
        "--allowed-repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Process only the given repo. Pass multiple times for multiple repos.",
    )
    parser.add_argument(
        "--skip-repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Never act on the given repo (for example a fixture). Repeatable.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Do everything except send macOS notifications.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    stats = run(args)
    print(
        f"scanned={stats.scanned} reran={stats.reran} "
        f"updated={stats.updated} notified={stats.notified}"
    )
    for err in stats.errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    sys.exit(main())
