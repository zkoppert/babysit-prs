"""Orchestration: the per-PR run loop and notification glue."""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

from checks import RequiredChecks, fetch_required_checks
from classify import Decision, classify
from constants import (
    ALERT_CI_STILL_FAILING,
    ALERT_LABELS,
    ALERT_ORDER,
    ALERT_UPDATE_FAILED,
    DEFAULT_NUDGE_WEEKDAYS,
    logger,
)
from dedup import advance_dedup_state, should_notify
from effects import (
    acquire_lock,
    load_state,
    notify,
    rerun_runs,
    save_state,
    update_branch,
)
from ghapi import (
    fetch_pr,
    fetch_review_comments,
    get_my_login,
    search_my_open_prs,
)


def _scan_window_days(active_days: int, nudge_weekdays: int) -> int:
    """Widen the scan window so a configured nudge stays reachable.

    The scan drops PRs not updated within ``active_days`` calendar days, but
    the nudge only fires for a PR idle ``nudge_weekdays`` weekdays. If the
    window were narrower than that weekday span (weekends included), every
    nudge-eligible PR would be filtered out before it was fetched, silently
    disabling the nudge. Return a window at least wide enough to reach it.
    ``active_days == 0`` means no window (scan everything), so leave it as-is.
    """
    if active_days <= 0 or nudge_weekdays <= 0:
        return active_days
    nudge_calendar_days = ((nudge_weekdays + 4) // 5) * 7 + 7
    return max(active_days, nudge_calendar_days)


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
    scan_days = _scan_window_days(args.active_days, args.nudge_weekdays)
    if scan_days != args.active_days:
        logger.debug(
            "widened scan window from %d to %d days to cover the %d-weekday nudge",
            args.active_days,
            scan_days,
            args.nudge_weekdays,
        )

    for repo, number in search_my_open_prs(
        set(args.owner), allowed, scan_days, set(args.skip_repo)
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
    """Notify once if the PR's state changed, then advance the de-dup state.

    The decision of whether to notify and how to advance the de-dup state is
    pure and lives in ``dedup``; this only wires it to the notification I/O.
    """
    updated_at = pr.get("updatedAt") or ""
    notify_now = should_notify(decision.alerts, updated_at, new_state)
    delivered = _emit(title, decision, pr_url, ctx, stats) if notify_now else False
    advance_dedup_state(
        new_state,
        alerts=decision.alerts,
        current_activity=decision.current_activity,
        updated_at=updated_at,
        notify_now=notify_now,
        delivered=delivered,
    )


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
