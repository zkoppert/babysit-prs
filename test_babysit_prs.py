# pylint: disable=missing-function-docstring,protected-access

"""Tests for the babysit_prs entry point: arg parsing and main()."""

from __future__ import annotations

from unittest import mock

import babysit_prs
import constants
import runner


def test_parse_args_owner_repeatable() -> None:
    ns = babysit_prs.parse_args(["--owner", "a", "--owner", "b", "--dry-run"])
    assert ns.owner == ["a", "b"]
    assert ns.dry_run is True


def test_parse_args_nudge_weekdays_default_and_override() -> None:
    assert babysit_prs.parse_args([]).nudge_weekdays == constants.DEFAULT_NUDGE_WEEKDAYS
    assert babysit_prs.parse_args(["--nudge-weekdays", "5"]).nudge_weekdays == 5


def test_main_returns_exit_code() -> None:
    ok = runner.BabysitStats(scanned=1)
    with mock.patch.object(babysit_prs, "run", return_value=ok):
        assert babysit_prs.main(["--dry-run"]) == 0
    err = runner.BabysitStats()
    err.errors.append("boom")
    with mock.patch.object(babysit_prs, "run", return_value=err):
        assert babysit_prs.main(["--dry-run"]) == 1
