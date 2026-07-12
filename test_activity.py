# pylint: disable=missing-function-docstring,protected-access

"""Tests for activity: bot detection, human activity, and weekday math."""

from __future__ import annotations

import pytest
from prfixtures import NOW, make_pr

import activity


@pytest.mark.parametrize(
    "login,expected",
    [
        ("dependabot[bot]", True),
        ("copilot-pull-request-reviewer[bot]", True),
        ("Copilot", True),
        ("github-actions", True),
        ("zkoppert", False),
        ("dr-robot-nux", False),
    ],
)
def test_is_bot(login: str, expected: bool) -> None:
    assert activity._is_bot(login) is expected


def test_latest_human_activity_excludes_me_and_noisy_bots() -> None:
    pr = make_pr(
        latestReviews=[
            {"author": {"login": "zkoppert"}, "submittedAt": "2026-07-10T09:00:00Z"},
            {
                "author": {"login": "dr-robot-nux"},
                "submittedAt": "2026-07-10T05:00:00Z",
            },
        ],
        comments=[
            {
                "author": {"login": "dependabot[bot]"},
                "createdAt": "2026-07-10T08:00:00Z",
            },
            {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"},
        ],
    )
    # My own 09:00 review and the dependabot 08:00 comment are excluded; the
    # newest remaining human activity is octocat at 06:00.
    assert activity.latest_human_activity(pr, "zkoppert") == "2026-07-10T06:00:00Z"


def test_latest_human_activity_includes_copilot_reviewer() -> None:
    pr = make_pr(
        latestReviews=[
            {
                "author": {"login": "copilot-pull-request-reviewer[bot]"},
                "submittedAt": "2026-07-10T09:00:00Z",
            }
        ],
        comments=[
            {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}
        ],
    )
    # The Copilot reviewer's comment is actionable, so it counts and wins.
    assert activity.latest_human_activity(pr, "zkoppert") == "2026-07-10T09:00:00Z"


def test_latest_human_activity_none_when_only_noisy_bots() -> None:
    pr = make_pr(
        latestReviews=[
            {
                "author": {"login": "github-actions"},
                "submittedAt": "2026-07-10T09:00:00Z",
            }
        ],
        comments=[
            {
                "author": {"login": "dependabot[bot]"},
                "createdAt": "2026-07-10T08:00:00Z",
            }
        ],
    )
    assert activity.latest_human_activity(pr, "zkoppert") is None


def test_latest_human_activity_includes_inline_review_replies() -> None:
    pr = make_pr(
        comments=[
            {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}
        ],
        reviewComments=[
            {"author": {"login": "dr-robot-nux"}, "createdAt": "2026-07-10T08:30:00Z"}
        ],
    )
    assert activity.latest_human_activity(pr, "zkoppert") == "2026-07-10T08:30:00Z"


def test_weekdays_since_excludes_weekends() -> None:
    # Fri 2026-07-10 -> Mon 2026-07-13 is one weekday (the weekend does not count).
    assert activity.weekdays_since("2026-07-10T00:00:00Z", NOW) == 1
    # Mon 2026-07-06 -> Mon 2026-07-13 is five weekdays.
    assert activity.weekdays_since("2026-07-06T00:00:00Z", NOW) == 5


def test_weekdays_since_unparseable_or_future_is_zero() -> None:
    assert activity.weekdays_since("not-a-date", NOW) == 0
    assert activity.weekdays_since("2026-07-20T00:00:00Z", NOW) == 0
