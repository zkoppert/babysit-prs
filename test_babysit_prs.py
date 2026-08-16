# pylint: disable=missing-function-docstring,protected-access

"""Tests for the babysit_prs entry point: arg parsing and main()."""

from __future__ import annotations

from unittest import mock

import pytest

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


def test_parse_args_review_environment_repos_are_targeted() -> None:
    args = babysit_prs.parse_args(
        [
            "--review-lab-repo",
            "Example/Repo",
            "--preview-repo",
            "Example/UI",
        ]
    )
    assert args.review_lab_repo == ["example/repo"]
    assert args.preview_repo == ["example/ui"]

    with pytest.raises(SystemExit):
        babysit_prs.parse_args(
            [
                "--review-lab-repo",
                "Example/Repo",
                "--preview-repo",
                "example/repo",
            ]
        )


def test_main_returns_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    ok = runner.BabysitStats(scanned=1)
    with mock.patch.object(babysit_prs, "run", return_value=ok):
        assert babysit_prs.main(["--dry-run"]) == 0
    assert "deployed=0" in capsys.readouterr().out
    err = runner.BabysitStats()
    err.errors.append("boom")
    with mock.patch.object(babysit_prs, "run", return_value=err):
        assert babysit_prs.main(["--dry-run"]) == 1
