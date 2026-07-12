"""Pure decision logic: turn a PR plus required checks into a Decision."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from activity import latest_human_activity, weekdays_since
from checks import (
    RequiredChecks,
    _has_unfinished_or_failing_check,
    required_check_status,
    rerunnable_run_ids,
)
from constants import (
    ALERT_CHANGES_REQUESTED,
    ALERT_CI_STILL_FAILING,
    ALERT_CONFLICTS,
    ALERT_NEW_COMMENT,
    ALERT_NUDGE_REVIEWERS,
    ALERT_READY,
    DEFAULT_NUDGE_WEEKDAYS,
)


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
