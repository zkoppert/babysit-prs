from __future__ import annotations

import logging
from pathlib import Path

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
