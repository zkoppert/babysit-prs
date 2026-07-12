# pylint: disable=missing-function-docstring,protected-access
"""Tests for runner notifications: de-dup, new-comment, and the reviewer nudge."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from unittest import mock

from prfixtures import ME, OLD, REQUIRED, _args, make_pr

import constants
import runner


def test_run_notifies_once_then_dedupes(tmp_path: Path) -> None:
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["notify"].return_value = True
        stats1 = runner.run(_args(tmp_path))
        stats2 = runner.run(_args(tmp_path))
        assert stats1.notified == 1
        assert stats2.notified == 0
        assert m["notify"].call_count == 1
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved[pr["url"]]["notified_sig"] == constants.ALERT_READY


def test_run_ready_flap_does_not_renotify(tmp_path: Path) -> None:
    """CLEAN -> BLOCKED -> CLEAN on the same head must notify only once.

    Regression for the persistent-alert flap re-notify bug: a green PR
    that briefly goes BLOCKED when the base moves must not re-ping.
    """
    clean = make_pr(mergeStateStatus="CLEAN")
    blocked = make_pr(mergeStateStatus="BLOCKED")
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_required_checks"].return_value = REQUIRED
        m["notify"].return_value = True
        m["fetch_pr"].side_effect = [clean, blocked, clean]
        runner.run(_args(tmp_path))
        runner.run(_args(tmp_path))
        runner.run(_args(tmp_path))
        assert m["notify"].call_count == 1


def test_run_new_comment_then_quiet(tmp_path: Path) -> None:
    pr = make_pr(
        mergeStateStatus="CLEAN",
        comments=[
            {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}
        ],
    )
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["notify"].return_value = True
        runner.run(_args(tmp_path))
        runner.run(_args(tmp_path))
        assert m["notify"].call_count == 1


def test_run_inline_reply_triggers_notification(tmp_path: Path) -> None:
    """An inline review-thread reply (no new submitted review) still pings."""
    pr = make_pr(mergeStateStatus="BLOCKED")
    replies = [{"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}]
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        fetch_review_comments=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["fetch_review_comments"].return_value = replies
        m["notify"].return_value = True
        runner.run(_args(tmp_path))
        m["notify"].assert_called_once()
        assert "New review comment" in m["notify"].call_args.args[1]


def test_run_no_notify_does_not_consume_dedup(tmp_path: Path) -> None:
    """--no-notify must not advance the notify de-dup state, or the event
    would be permanently swallowed. Regression for the state-leak bug."""
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["notify"].return_value = True
        runner.run(_args(tmp_path, no_notify=True))
        assert m["notify"].call_count == 0
        runner.run(_args(tmp_path, no_notify=False))
        assert m["notify"].call_count == 1


def test_nudge_refires_after_activity_then_idle(tmp_path: Path) -> None:
    """nudge -> reviewer comment -> idle again must re-nudge (not one-shot).

    Regression for the finding that the persistent-signature preservation
    silenced every nudge after the first reviewer touch. Driven at the
    _process_pr layer where the de-dup lives, with an injected now.
    """
    utc = datetime.timezone.utc
    ctx = runner.RunContext(
        my_login=ME,
        dry_run=False,
        no_notify=False,
        state={},
        now=datetime.datetime(2026, 7, 13, 12, 0, tzinfo=utc),  # Mon
        nudge_weekdays=3,
    )
    ctx.required_cache["o/r@main"] = REQUIRED  # avoid a gh call
    stats = runner.BabysitStats()
    with mock.patch.object(
        runner, "notify", return_value=True
    ) as notify_mock, mock.patch.object(
        runner, "fetch_review_comments", return_value=[]
    ):
        # Run 1: idle 5 weekdays -> nudge fires.
        runner._process_pr(
            make_pr(mergeStateStatus="BLOCKED", updatedAt=OLD), "o/r", 1, ctx, stats
        )
        assert notify_mock.call_count == 1
        assert (
            constants.ALERT_LABELS[constants.ALERT_NUDGE_REVIEWERS]
            in notify_mock.call_args.args[1]
        )

        # Run 2: reviewer comments today -> updatedAt bumps, new-comment fires.
        ctx.now = datetime.datetime(2026, 7, 14, 12, 0, tzinfo=utc)  # Tue
        commented = make_pr(
            mergeStateStatus="BLOCKED",
            updatedAt="2026-07-14T00:00:00Z",
            comments=[
                {"author": {"login": "octocat"}, "createdAt": "2026-07-14T00:00:00Z"}
            ],
        )
        runner._process_pr(commented, "o/r", 1, ctx, stats)
        assert notify_mock.call_count == 2

        # Run 3: idle again 4 weekdays later, no further activity -> re-nudge.
        ctx.now = datetime.datetime(2026, 7, 20, 12, 0, tzinfo=utc)  # next Mon
        runner._process_pr(commented, "o/r", 1, ctx, stats)
        assert notify_mock.call_count == 3


def test_run_notify_failure_is_retried(tmp_path: Path) -> None:
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["notify"].return_value = False  # delivery fails
        runner.run(_args(tmp_path))
        m["notify"].return_value = True  # recovers
        runner.run(_args(tmp_path))
        assert m["notify"].call_count == 2  # retried, not consumed


def test_run_notifies_per_pr_no_digest(tmp_path: Path) -> None:
    prs = [(f"o/r{i}", i) for i in range(12)]

    def fake_fetch(repo: str, number: int) -> dict[str, Any]:
        return make_pr(number=number, url=f"https://github.com/{repo}/pull/{number}")

    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = prs
        m["fetch_pr"].side_effect = fake_fetch
        m["fetch_required_checks"].return_value = REQUIRED
        m["notify"].return_value = True
        stats = runner.run(_args(tmp_path))
        assert m["notify"].call_count == 12
        assert stats.notified == 12
        for call in m["notify"].call_args_list:
            assert call.args[2].startswith("https://github.com/")
