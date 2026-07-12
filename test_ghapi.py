# pylint: disable=missing-function-docstring,protected-access

"""Tests for ghapi: gh CLI wrappers, PR search, and PR fetch."""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest

import ghapi


def test_fetch_review_comments_normalizes_rest_shape() -> None:
    pages = json.dumps(
        [
            [
                {"user": {"login": "octocat"}, "created_at": "2026-07-10T06:00:00Z"},
                {"user": {"login": "zkoppert"}, "created_at": "2026-07-10T07:00:00Z"},
            ]
        ]
    )
    with mock.patch.object(ghapi, "run_gh", return_value=pages) as gh:
        out = ghapi.fetch_review_comments("o/r", 1)
    assert out == [
        {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"},
        {"author": {"login": "zkoppert"}, "createdAt": "2026-07-10T07:00:00Z"},
    ]
    assert "/repos/o/r/pulls/1/comments" in gh.call_args.args[0]


def test_fetch_review_comments_empty_on_error() -> None:

    with mock.patch.object(
        ghapi, "run_gh", side_effect=subprocess.CalledProcessError(1, "gh")
    ):
        assert ghapi.fetch_review_comments("o/r", 1) == []


def test_search_queries_author_and_assignee() -> None:
    with mock.patch.object(ghapi, "run_gh", return_value="[]") as run_mock:
        ghapi.search_my_open_prs(set(), set(), active_days=7)
    roles = [
        arg
        for call in run_mock.call_args_list
        for arg in call.args[0]
        if arg in ("--author=@me", "--assignee=@me")
    ]
    assert set(roles) == {"--author=@me", "--assignee=@me"}


def test_search_applies_active_window() -> None:
    with mock.patch.object(ghapi, "run_gh", return_value="[]") as run_mock:
        ghapi.search_my_open_prs({"zkoppert"}, set(), active_days=7)
    called = run_mock.call_args.args[0]
    assert any(a.startswith("updated:>=") for a in called)
    assert "--owner=zkoppert" in called


def test_search_no_window_when_zero() -> None:
    with mock.patch.object(ghapi, "run_gh", return_value="[]") as run_mock:
        ghapi.search_my_open_prs({"zkoppert"}, set(), active_days=0)
    assert not any(a.startswith("updated:>=") for a in run_mock.call_args.args[0])


def test_search_no_owner_filter_when_empty() -> None:
    with mock.patch.object(ghapi, "run_gh", return_value="[]") as run_mock:
        ghapi.search_my_open_prs(set(), set())
    assert not any(a.startswith("--owner=") for a in run_mock.call_args.args[0])


def test_search_unions_and_dedups_author_assignee() -> None:
    author_rows = json.dumps([{"number": 1, "repository": {"nameWithOwner": "o/a"}}])
    assignee_rows = json.dumps(
        [
            {"number": 1, "repository": {"nameWithOwner": "o/a"}},  # dup of author
            {"number": 2, "repository": {"nameWithOwner": "o/b"}},
        ]
    )
    with mock.patch.object(ghapi, "run_gh", side_effect=[author_rows, assignee_rows]):
        prs = ghapi.search_my_open_prs(set(), set())
    assert prs == [("o/a", 1), ("o/b", 2)]  # union, deduped, author first


def test_search_filters_allowed_and_skip() -> None:
    rows = json.dumps(
        [
            {"number": 1, "repository": {"nameWithOwner": "zkoppert/a"}},
            {"number": 2, "repository": {"nameWithOwner": "zkoppert/b"}},
            {"number": 3, "repository": {"nameWithOwner": "zkoppert/fixture"}},
        ]
    )
    with mock.patch.object(ghapi, "run_gh", return_value=rows):
        allowed = ghapi.search_my_open_prs({"zkoppert"}, {"zkoppert/b"})
        skipped = ghapi.search_my_open_prs(
            {"zkoppert"}, set(), skip={"zkoppert/fixture"}
        )
    assert allowed == [("zkoppert/b", 2)]
    assert ("zkoppert/fixture", 3) not in skipped
    assert ("zkoppert/a", 1) in skipped


def test_get_my_login_success_and_error() -> None:
    with mock.patch.object(ghapi, "run_gh", return_value='{"login": "octocat"}'):
        assert ghapi.get_my_login() == "octocat"
    with mock.patch.object(ghapi, "run_gh", return_value='{"nope": 1}'):
        with pytest.raises(LookupError):
            ghapi.get_my_login()


def test_fetch_pr_success_and_error() -> None:
    with mock.patch.object(ghapi, "run_gh", return_value='{"number": 5}'):
        assert ghapi.fetch_pr("o/r", 5) == {"number": 5}
    with mock.patch.object(
        ghapi, "run_gh", side_effect=subprocess.CalledProcessError(1, "gh")
    ):
        assert ghapi.fetch_pr("o/r", 5) is None
    with mock.patch.object(ghapi, "run_gh", return_value="not json"):
        assert ghapi.fetch_pr("o/r", 5) is None


def test_fetch_review_comments_paginated_pages() -> None:
    pages = json.dumps(
        [
            [{"user": {"login": "a"}, "created_at": "2026-07-10T01:00:00Z"}],
            [{"user": {"login": "b"}, "created_at": "2026-07-10T02:00:00Z"}],
        ]
    )
    with mock.patch.object(ghapi, "run_gh", return_value=pages):
        out = ghapi.fetch_review_comments("o/r", 1)
    assert [c["author"]["login"] for c in out] == ["a", "b"]
