# pylint: disable=missing-function-docstring,protected-access
"""Tests for runner: signature, scan window, and the run-loop mechanics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from prfixtures import HEAD, ME, REQUIRED, _args, check_run, make_pr

import constants
import effects
import runner


def test_signature_empty_and_excludes_new_comment() -> None:
    assert runner.signature([]) == ""
    assert runner.signature([constants.ALERT_NEW_COMMENT]) == ""
    assert runner.signature(
        [constants.ALERT_READY, constants.ALERT_NEW_COMMENT]
    ) == runner.signature([constants.ALERT_READY])


def test_signature_order_independent() -> None:
    assert runner.signature(
        [constants.ALERT_READY, constants.ALERT_CONFLICTS]
    ) == runner.signature([constants.ALERT_CONFLICTS, constants.ALERT_READY])


def test_scan_window_widens_to_keep_nudge_reachable() -> None:
    # A window narrower than the nudge's weekday span (weekends included) would
    # drop every nudge-eligible PR before it was fetched, so it is widened.
    assert runner._scan_window_days(1, 3) == 14  # too small -> covers 3 weekdays
    assert runner._scan_window_days(14, 15) == 28  # big nudge -> even wider window
    # Left alone when already wide enough, disabled, or unbounded.
    assert runner._scan_window_days(14, 3) == 14  # defaults unchanged
    assert runner._scan_window_days(30, 3) == 30  # explicit larger window kept
    assert runner._scan_window_days(1, 0) == 1  # nudge disabled, no widening
    assert runner._scan_window_days(0, 3) == 0  # no limit stays no limit


def test_run_widens_scan_window_for_nudge(tmp_path: Path) -> None:
    # run() must pass the widened window to the search, not the raw --active-days.
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = ME
        m["search_my_open_prs"].return_value = []
        runner.run(_args(tmp_path, active_days=1, nudge_weekdays=3))
    assert m["search_my_open_prs"].call_args.args[2] == 14


def test_run_update_priority_skips_rerun(tmp_path: Path) -> None:
    pr = make_pr(
        mergeStateStatus="BEHIND", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        rerun_runs=mock.DEFAULT,
        update_branch=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["update_branch"].return_value = True
        stats = runner.run(_args(tmp_path))
        m["update_branch"].assert_called_once()
        m["rerun_runs"].assert_not_called()
        assert stats.updated == 1
        assert stats.reran == 0


def test_run_reruns_specific_runs_when_not_behind(tmp_path: Path) -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        rerun_runs=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["rerun_runs"].return_value = True
        stats = runner.run(_args(tmp_path))
        m["rerun_runs"].assert_called_once()
        assert m["rerun_runs"].call_args.args[1] == [999]
        assert stats.reran == 1
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved[pr["url"]]["rerun_head"] == HEAD


def test_run_rerun_failure_alerts_and_stops_retrying(tmp_path: Path) -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        rerun_runs=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["rerun_runs"].return_value = False  # e.g. missing Actions permission
        m["notify"].return_value = True
        stats = runner.run(_args(tmp_path))
        m["notify"].assert_called_once()
        assert "Failing CI" in m["notify"].call_args.args[1]
        assert stats.reran == 0
    # rerun_head advanced so the next run does not retry the doomed re-run forever
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved[pr["url"]]["rerun_head"] == HEAD
    pr = make_pr(mergeStateStatus="BEHIND")
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        update_branch=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["update_branch"].return_value = False
        m["notify"].return_value = True
        runner.run(_args(tmp_path))
        m["notify"].assert_called_once()
        title, subtitle, url = m["notify"].call_args.args
        assert title == "o/r#1"
        assert "Branch update failed" in subtitle
        assert url == "https://github.com/o/r/pull/1"


def test_run_isolates_per_pr_errors(tmp_path: Path) -> None:
    good = make_pr(
        number=2, url="https://github.com/o/r/pull/2", mergeStateStatus="CLEAN"
    )

    def fetch(repo: str, number: int) -> dict[str, Any]:
        if number == 1:
            raise FileNotFoundError("gh missing")
        return good

    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        notify=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1), ("o/r", 2)]
        m["fetch_pr"].side_effect = fetch
        m["fetch_required_checks"].return_value = REQUIRED
        m["notify"].return_value = True
        stats = runner.run(_args(tmp_path))
        assert stats.scanned == 2
        assert stats.notified == 1  # the good PR still processed
        assert stats.errors  # the bad PR recorded


def test_run_records_error_when_login_fails(tmp_path: Path) -> None:
    with mock.patch.object(runner, "get_my_login", side_effect=LookupError("boom")):
        stats = runner.run(_args(tmp_path))
    assert stats.errors
    assert stats.scanned == 0


def test_run_records_save_error(tmp_path: Path) -> None:
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        runner,
        get_my_login=mock.DEFAULT,
        search_my_open_prs=mock.DEFAULT,
        fetch_pr=mock.DEFAULT,
        fetch_required_checks=mock.DEFAULT,
        notify=mock.DEFAULT,
        save_state=mock.DEFAULT,
    ) as m:
        m["get_my_login"].return_value = "zkoppert"
        m["search_my_open_prs"].return_value = [("o/r", 1)]
        m["fetch_pr"].return_value = pr
        m["fetch_required_checks"].return_value = REQUIRED
        m["notify"].return_value = True
        m["save_state"].side_effect = OSError("disk full")
        stats = runner.run(_args(tmp_path))
        assert any("save state" in e for e in stats.errors)


def test_run_skips_when_locked(tmp_path: Path) -> None:
    lock = effects.acquire_lock((tmp_path / "state.json").with_suffix(".lock"))
    assert lock is not None
    try:
        with mock.patch.object(runner, "get_my_login") as login_mock:
            stats = runner.run(_args(tmp_path))
        login_mock.assert_not_called()  # bailed before doing any work
        assert stats.scanned == 0
    finally:
        lock.close()


def test_run_dry_run_skips_state_write(tmp_path: Path) -> None:
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
        runner.run(_args(tmp_path, dry_run=True))
        m["notify"].assert_called_once()  # invoked with dry_run=True for the log line
    assert not (tmp_path / "state.json").exists()
