"""Pure notification de-dup: decide whether to notify and advance the state.

Kept separate from the run loop and from I/O so the de-dup rules (which have
been a repeat source of subtle bugs: persistent-alert flap re-notify, one-shot
nudges) can be unit-tested directly without mocking ``gh`` or ``notify``.
"""

from __future__ import annotations

from constants import ALERT_NEW_COMMENT, ALERT_NUDGE_REVIEWERS


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


def _nudge_event(alerts: list[str], updated_at: str, nudged_at: str) -> bool:
    """Return True when a nudge should fire for a fresh idle episode.

    The nudge is event-like: it re-fires once per idle period, keyed on the
    ``updatedAt`` it last fired at, so a standing nudge stays quiet but a fresh
    idle stretch (new ``updatedAt``) fires again.
    """
    return ALERT_NUDGE_REVIEWERS in alerts and updated_at != nudged_at


def should_notify(alerts: list[str], updated_at: str, state: dict) -> bool:
    """Return True when the PR's current alerts warrant a notification.

    Notify when the persistent signature changed, or on an event-like alert
    (a new review comment, or a nudge for a fresh idle episode).
    """
    sig = signature(alerts)
    return (
        (bool(sig) and sig != state.get("notified_sig", ""))
        or ALERT_NEW_COMMENT in alerts
        or _nudge_event(alerts, updated_at, state.get("nudged_at", ""))
    )


def advance_dedup_state(
    state: dict,
    *,
    alerts: list[str],
    current_activity: str | None,
    updated_at: str,
    notify_now: bool,
    delivered: bool,
) -> None:
    """Advance the per-PR de-dup fields in ``state`` after a notify attempt.

    A notification that was needed but not delivered leaves the state
    untouched, so it is retried on the next run. Otherwise the persistent
    signature is advanced (preserving a previously-notified signature when the
    current one is empty, so an alert that transiently disappears and reappears
    on the same commit is not re-notified), the latest seen activity is
    recorded, and a fired nudge is stamped with the ``updatedAt`` it fired at.
    """
    if notify_now and not delivered:
        return
    state["notified_sig"] = signature(alerts) or state.get("notified_sig", "")
    if current_activity:
        state["last_activity"] = current_activity
    if ALERT_NUDGE_REVIEWERS in alerts:
        state["nudged_at"] = updated_at
