# pylint: disable=missing-function-docstring,protected-access

"""Tests for checks: required-check discovery and rollup predicates."""

from __future__ import annotations

import json
import subprocess
from unittest import mock

from prfixtures import (
    REQUIRED,
    RUN_URL,
    UNKNOWN,
    check_run,
    make_pr,
    status_context,
)

import checks


def test_check_name_prefers_checkrun_then_context() -> None:
    assert checks.check_name(check_run("test", "SUCCESS")) == "test"
    assert checks.check_name(status_context("ci", "SUCCESS")) == "ci"


def test_check_pending_and_failed_for_checkrun() -> None:
    assert checks.check_is_pending(check_run("t", "", "IN_PROGRESS")) is True
    assert checks.check_is_failed(check_run("t", "", "IN_PROGRESS")) is False
    assert checks.check_is_failed(check_run("t", "FAILURE")) is True
    assert checks.check_is_failed(check_run("t", "SUCCESS")) is False


def test_check_failed_for_status_context() -> None:
    assert checks.check_is_failed(status_context("ci", "FAILURE")) is True
    assert checks.check_is_pending(status_context("ci", "PENDING")) is True
    assert checks.check_is_failed(status_context("ci", "SUCCESS")) is False


def test_run_id_from_details_url() -> None:
    assert checks._run_id_from_details_url(RUN_URL) == 999
    assert checks._run_id_from_details_url("https://example.com/nope") is None
    assert checks._run_id_from_details_url("") is None


def test_rerunnable_run_ids_checkruns_only_deduped() -> None:
    entries = [
        check_run(
            "ci", "FAILURE", details_url="https://github.com/o/r/actions/runs/7/job/1"
        ),
        check_run(
            "ci2", "FAILURE", details_url="https://github.com/o/r/actions/runs/7/job/2"
        ),
        status_context("external", "FAILURE"),
    ]
    assert checks.rerunnable_run_ids(entries) == [7]


def test_required_check_status_unknown_is_inert() -> None:
    pr = make_pr(statusCheckRollup=[check_run("ci", "FAILURE")])
    assert checks.required_check_status(pr, UNKNOWN) == ([], False)


def test_required_check_status_returns_failed_required_entries() -> None:
    pr = make_pr(
        statusCheckRollup=[
            check_run("ci", "FAILURE"),
            check_run("lint", "FAILURE"),  # not required
        ]
    )
    failed, pending = checks.required_check_status(pr, REQUIRED)
    assert [checks.check_name(e) for e in failed] == ["ci"]
    assert pending is False


def test_required_check_status_pending_required() -> None:
    pr = make_pr(statusCheckRollup=[check_run("ci", "", "IN_PROGRESS")])
    failed, pending = checks.required_check_status(pr, REQUIRED)
    assert failed == []
    assert pending is True


def test_fetch_required_checks_from_protection() -> None:
    payload = json.dumps(
        {"strict": True, "contexts": ["ci"], "checks": [{"context": "build"}]}
    )
    with mock.patch.object(checks, "run_gh", return_value=payload):
        rc = checks.fetch_required_checks("o/r", "main")
    assert rc.known is True
    assert rc.strict is True
    assert rc.contexts == {"ci", "build"}


def test_fetch_required_checks_falls_back_to_rules() -> None:
    rules = json.dumps(
        [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "test"}],
                },
            }
        ]
    )

    def gh(args, timeout=60):
        if "protection" in args[1]:
            raise subprocess.CalledProcessError(1, "gh")
        return rules

    with mock.patch.object(checks, "run_gh", side_effect=gh):
        rc = checks.fetch_required_checks("o/r", "main")
    assert rc.contexts == {"test"}
    assert rc.strict is True


def test_fetch_required_checks_unknown_when_both_fail() -> None:
    with mock.patch.object(
        checks, "run_gh", side_effect=subprocess.CalledProcessError(1, "gh")
    ):
        rc = checks.fetch_required_checks("o/r", "main")
    assert rc.known is False


def test_fetch_required_checks_no_base() -> None:
    assert checks.fetch_required_checks("o/r", "").known is False


def test_fetch_required_checks_unions_protection_and_rules() -> None:
    protection = json.dumps({"strict": False, "contexts": ["lint"], "checks": []})
    rules = json.dumps(
        [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "test"}],
                },
            }
        ]
    )

    def gh(args, timeout=60):
        return protection if "protection" in args[1] else rules

    with mock.patch.object(checks, "run_gh", side_effect=gh):
        rc = checks.fetch_required_checks("o/r", "main")
    assert rc.contexts == {"lint", "test"}  # both sources unioned
    assert rc.strict is True  # strict if either source is strict
