"""Detecting notify-worthy human (and Copilot) activity, and weekday math."""

from __future__ import annotations

import datetime
from typing import Any


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
