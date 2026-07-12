# pylint: disable=missing-function-docstring,protected-access
"""Tests for dedup: the pure notification de-dup state machine."""

from __future__ import annotations

import constants
import dedup


def test_signature_empty_and_excludes_new_comment() -> None:
    assert dedup.signature([]) == ""
    assert dedup.signature([constants.ALERT_NEW_COMMENT]) == ""
    assert dedup.signature([constants.ALERT_NUDGE_REVIEWERS]) == ""
    assert dedup.signature(
        [constants.ALERT_READY, constants.ALERT_NEW_COMMENT]
    ) == dedup.signature([constants.ALERT_READY])


def test_signature_order_independent() -> None:
    assert dedup.signature(
        [constants.ALERT_READY, constants.ALERT_CONFLICTS]
    ) == dedup.signature([constants.ALERT_CONFLICTS, constants.ALERT_READY])


def test_should_notify_on_new_signature_then_deduped() -> None:
    alerts = [constants.ALERT_READY]
    assert dedup.should_notify(alerts, "", {}) is True
    # Once the signature is recorded, the same alerts do not notify again.
    state = {"notified_sig": dedup.signature(alerts)}
    assert dedup.should_notify(alerts, "", state) is False


def test_should_notify_empty_alerts_is_false() -> None:
    assert dedup.should_notify([], "", {}) is False


def test_should_notify_new_comment_always_fires() -> None:
    # A new-comment alert is event-like: it notifies even though it never
    # contributes to the persistent signature, so it is not deduped by it.
    state = {"notified_sig": ""}
    assert dedup.should_notify([constants.ALERT_NEW_COMMENT], "", state) is True


def test_should_notify_nudge_only_on_new_updated_at() -> None:
    alerts = [constants.ALERT_NUDGE_REVIEWERS]
    # A standing nudge (same updatedAt already stamped) stays quiet.
    assert dedup.should_notify(alerts, "T1", {"nudged_at": "T1"}) is False
    # A fresh idle episode (new updatedAt) fires again.
    assert dedup.should_notify(alerts, "T2", {"nudged_at": "T1"}) is True


def test_advance_records_signature_activity_and_nudge() -> None:
    state: dict = {"notified_sig": "", "last_activity": "", "nudged_at": ""}
    dedup.advance_dedup_state(
        state,
        alerts=[constants.ALERT_READY, constants.ALERT_NUDGE_REVIEWERS],
        current_activity="2026-07-10T00:00:00Z",
        updated_at="2026-07-11T00:00:00Z",
        notify_now=True,
        delivered=True,
    )
    assert state["notified_sig"] == constants.ALERT_READY
    assert state["last_activity"] == "2026-07-10T00:00:00Z"
    assert state["nudged_at"] == "2026-07-11T00:00:00Z"


def test_advance_skips_when_notify_needed_but_not_delivered() -> None:
    # A failed delivery must not advance the state, so it is retried next run.
    state = {"notified_sig": "old", "last_activity": "a", "nudged_at": "n"}
    dedup.advance_dedup_state(
        state,
        alerts=[constants.ALERT_READY],
        current_activity="b",
        updated_at="new",
        notify_now=True,
        delivered=False,
    )
    assert state == {"notified_sig": "old", "last_activity": "a", "nudged_at": "n"}


def test_advance_preserves_signature_when_current_is_empty() -> None:
    # An event-only cycle (empty persistent signature) must not clear a
    # previously-notified signature, or a flapping alert would re-notify.
    state = {"notified_sig": "ready-to-merge", "last_activity": "", "nudged_at": ""}
    dedup.advance_dedup_state(
        state,
        alerts=[constants.ALERT_NEW_COMMENT],
        current_activity=None,
        updated_at="",
        notify_now=True,
        delivered=True,
    )
    assert state["notified_sig"] == "ready-to-merge"
