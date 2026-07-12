"""Shared test fixtures: PR/check factories and deterministic constants."""

# pylint: disable=missing-function-docstring
from __future__ import annotations

import argparse
import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import checks
import runner

REQUIRED = checks.RequiredChecks(contexts={"ci"}, strict=True)
REQUIRED_LOOSE = checks.RequiredChecks(contexts={"ci"}, strict=False)
UNKNOWN = checks.RequiredChecks(contexts=None, strict=False)
HEAD = "abc123"
RUN_URL = "https://github.com/o/r/actions/runs/999/job/1"
ME = "zkoppert"
NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.timezone.utc)
OLD = "2026-07-06T00:00:00Z"  # the prior Monday: 5 weekdays before NOW


def make_pr(**overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "number": 1,
        "title": "A change",
        "url": "https://github.com/o/r/pull/1",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": HEAD,
        "baseRefName": "main",
        "reviewDecision": "",
        "statusCheckRollup": [],
        "latestReviews": [],
        "comments": [],
        "reviewComments": [],
        "author": {"login": ME},
    }
    pr.update(overrides)
    return pr


def check_run(
    name: str, conclusion: str, status: str = "COMPLETED", details_url: str = RUN_URL
) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": status,
        "detailsUrl": details_url,
    }


def status_context(context: str, state: str) -> dict[str, Any]:
    return {"__typename": "StatusContext", "context": context, "state": state}


def _args(tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    ns = argparse.Namespace(
        owner=[],
        state_file=tmp_path / "state.json",
        dry_run=False,
        allowed_repo=[],
        skip_repo=[],
        active_days=14,
        nudge_weekdays=3,
        no_notify=False,
        verbose=False,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _patch_run(
    pr_or_side: Any, required: checks.RequiredChecks = REQUIRED, **extra: Any
):
    fetch_kwargs = (
        {"side_effect": pr_or_side}
        if isinstance(pr_or_side, list)
        else {"return_value": pr_or_side}
    )
    prs = extra.pop("prs", [("o/r", 1)])
    patches = [
        mock.patch.object(runner, "get_my_login", return_value="zkoppert"),
        mock.patch.object(runner, "search_my_open_prs", return_value=prs),
        mock.patch.object(runner, "fetch_pr", **fetch_kwargs),
        mock.patch.object(runner, "fetch_required_checks", return_value=required),
    ]
    return patches
