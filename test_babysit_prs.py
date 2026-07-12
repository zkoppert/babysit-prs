"""Tests for babysit_prs.

The strategy mirrors triage-dependabot/tests.py: exercise the pure
``classify`` decision tree one branch at a time, then drive ``run()``
end-to-end with every gh/notify boundary mocked. Several tests are
regressions for bugs caught in multi-model review and the first live
trial (persistent-alert flap re-notify; ``--no-notify`` consuming
de-dup state; re-running non-required checks).
"""

# Test names are self-documenting and several tests deliberately exercise
# private helpers, so relax the docstring and protected-access checks here.
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import babysit_prs as bp
import pytest

REQUIRED = bp.RequiredChecks(contexts={"ci"}, strict=True)
REQUIRED_LOOSE = bp.RequiredChecks(contexts={"ci"}, strict=False)
UNKNOWN = bp.RequiredChecks(contexts=None, strict=False)
HEAD = "abc123"
RUN_URL = "https://github.com/o/r/actions/runs/999/job/1"
ME = "zkoppert"
# A fixed "now" and an old timestamp for deterministic nudge tests.
NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.timezone.utc)
OLD = "2026-07-06T00:00:00Z"  # the prior Monday: 5 weekdays before NOW
# Captured before the autouse stub patches the module attribute, so the
# direct unit tests below still exercise the real implementation.
_REAL_FETCH_REVIEW_COMMENTS = bp.fetch_review_comments


@pytest.fixture(autouse=True)
def _stub_review_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep run() tests from making a real REST call per PR; tests that
    # exercise inline replies override this within the test.
    monkeypatch.setattr(bp, "fetch_review_comments", lambda repo, number: [])


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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


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
    assert bp._is_bot(login) is expected


def test_check_name_prefers_checkrun_then_context() -> None:
    assert bp.check_name(check_run("test", "SUCCESS")) == "test"
    assert bp.check_name(status_context("ci", "SUCCESS")) == "ci"


def test_check_pending_and_failed_for_checkrun() -> None:
    assert bp.check_is_pending(check_run("t", "", "IN_PROGRESS")) is True
    assert bp.check_is_failed(check_run("t", "", "IN_PROGRESS")) is False
    assert bp.check_is_failed(check_run("t", "FAILURE")) is True
    assert bp.check_is_failed(check_run("t", "SUCCESS")) is False


def test_check_failed_for_status_context() -> None:
    assert bp.check_is_failed(status_context("ci", "FAILURE")) is True
    assert bp.check_is_pending(status_context("ci", "PENDING")) is True
    assert bp.check_is_failed(status_context("ci", "SUCCESS")) is False


def test_run_id_from_details_url() -> None:
    assert bp._run_id_from_details_url(RUN_URL) == 999
    assert bp._run_id_from_details_url("https://example.com/nope") is None
    assert bp._run_id_from_details_url("") is None


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
    assert bp.rerunnable_run_ids(entries) == [7]


def test_required_check_status_unknown_is_inert() -> None:
    pr = make_pr(statusCheckRollup=[check_run("ci", "FAILURE")])
    assert bp.required_check_status(pr, UNKNOWN) == ([], False)


def test_required_check_status_returns_failed_required_entries() -> None:
    pr = make_pr(
        statusCheckRollup=[
            check_run("ci", "FAILURE"),
            check_run("lint", "FAILURE"),  # not required
        ]
    )
    failed, pending = bp.required_check_status(pr, REQUIRED)
    assert [bp.check_name(e) for e in failed] == ["ci"]
    assert pending is False


def test_required_check_status_pending_required() -> None:
    pr = make_pr(statusCheckRollup=[check_run("ci", "", "IN_PROGRESS")])
    failed, pending = bp.required_check_status(pr, REQUIRED)
    assert failed == []
    assert pending is True


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
    assert bp.latest_human_activity(pr, "zkoppert") == "2026-07-10T06:00:00Z"


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
    assert bp.latest_human_activity(pr, "zkoppert") == "2026-07-10T09:00:00Z"


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
    assert bp.latest_human_activity(pr, "zkoppert") is None


def test_latest_human_activity_includes_inline_review_replies() -> None:
    pr = make_pr(
        comments=[
            {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}
        ],
        reviewComments=[
            {"author": {"login": "dr-robot-nux"}, "createdAt": "2026-07-10T08:30:00Z"}
        ],
    )
    assert bp.latest_human_activity(pr, "zkoppert") == "2026-07-10T08:30:00Z"


def test_fetch_review_comments_normalizes_rest_shape() -> None:
    pages = json.dumps(
        [
            [
                {"user": {"login": "octocat"}, "created_at": "2026-07-10T06:00:00Z"},
                {"user": {"login": "zkoppert"}, "created_at": "2026-07-10T07:00:00Z"},
            ]
        ]
    )
    with mock.patch.object(bp, "run_gh", return_value=pages) as gh:
        out = _REAL_FETCH_REVIEW_COMMENTS("o/r", 1)
    assert out == [
        {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"},
        {"author": {"login": "zkoppert"}, "createdAt": "2026-07-10T07:00:00Z"},
    ]
    assert "/repos/o/r/pulls/1/comments" in gh.call_args.args[0]


def test_fetch_review_comments_empty_on_error() -> None:
    import subprocess as sp

    with mock.patch.object(bp, "run_gh", side_effect=sp.CalledProcessError(1, "gh")):
        assert _REAL_FETCH_REVIEW_COMMENTS("o/r", 1) == []


def test_signature_empty_and_excludes_new_comment() -> None:
    assert bp.signature([]) == ""
    assert bp.signature([bp.ALERT_NEW_COMMENT]) == ""
    assert bp.signature([bp.ALERT_READY, bp.ALERT_NEW_COMMENT]) == bp.signature(
        [bp.ALERT_READY]
    )


def test_signature_order_independent() -> None:
    assert bp.signature([bp.ALERT_READY, bp.ALERT_CONFLICTS]) == bp.signature(
        [bp.ALERT_CONFLICTS, bp.ALERT_READY]
    )


# ---------------------------------------------------------------------------
# search_my_open_prs
# ---------------------------------------------------------------------------


def test_search_queries_author_and_assignee() -> None:
    with mock.patch.object(bp, "run_gh", return_value="[]") as run_mock:
        bp.search_my_open_prs(set(), set(), active_days=7)
    roles = [
        arg
        for call in run_mock.call_args_list
        for arg in call.args[0]
        if arg in ("--author=@me", "--assignee=@me")
    ]
    assert set(roles) == {"--author=@me", "--assignee=@me"}


def test_search_applies_active_window() -> None:
    with mock.patch.object(bp, "run_gh", return_value="[]") as run_mock:
        bp.search_my_open_prs({"zkoppert"}, set(), active_days=7)
    called = run_mock.call_args.args[0]
    assert any(a.startswith("updated:>=") for a in called)
    assert "--owner=zkoppert" in called


def test_search_no_window_when_zero() -> None:
    with mock.patch.object(bp, "run_gh", return_value="[]") as run_mock:
        bp.search_my_open_prs({"zkoppert"}, set(), active_days=0)
    assert not any(a.startswith("updated:>=") for a in run_mock.call_args.args[0])


def test_search_no_owner_filter_when_empty() -> None:
    with mock.patch.object(bp, "run_gh", return_value="[]") as run_mock:
        bp.search_my_open_prs(set(), set())
    assert not any(a.startswith("--owner=") for a in run_mock.call_args.args[0])


def test_search_unions_and_dedups_author_assignee() -> None:
    author_rows = json.dumps([{"number": 1, "repository": {"nameWithOwner": "o/a"}}])
    assignee_rows = json.dumps(
        [
            {"number": 1, "repository": {"nameWithOwner": "o/a"}},  # dup of author
            {"number": 2, "repository": {"nameWithOwner": "o/b"}},
        ]
    )
    with mock.patch.object(bp, "run_gh", side_effect=[author_rows, assignee_rows]):
        prs = bp.search_my_open_prs(set(), set())
    assert prs == [("o/a", 1), ("o/b", 2)]  # union, deduped, author first


def test_search_filters_allowed_and_skip() -> None:
    rows = json.dumps(
        [
            {"number": 1, "repository": {"nameWithOwner": "zkoppert/a"}},
            {"number": 2, "repository": {"nameWithOwner": "zkoppert/b"}},
            {"number": 3, "repository": {"nameWithOwner": "zkoppert/fixture"}},
        ]
    )
    with mock.patch.object(bp, "run_gh", return_value=rows):
        allowed = bp.search_my_open_prs({"zkoppert"}, {"zkoppert/b"})
        skipped = bp.search_my_open_prs({"zkoppert"}, set(), skip={"zkoppert/fixture"})
    assert allowed == [("zkoppert/b", 2)]
    assert ("zkoppert/fixture", 3) not in skipped
    assert ("zkoppert/a", 1) in skipped


# ---------------------------------------------------------------------------
# classify decision tree
# ---------------------------------------------------------------------------


def test_classify_reruns_failed_required_once_then_alerts() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    first = bp.classify(pr, REQUIRED, "zkoppert", {})
    assert first.do_rerun is True
    assert first.rerun_run_ids == [999]
    assert bp.ALERT_CI_STILL_FAILING not in first.alerts

    second = bp.classify(pr, REQUIRED, "zkoppert", {"rerun_head": HEAD})
    assert second.do_rerun is False
    assert bp.ALERT_CI_STILL_FAILING in second.alerts


def test_classify_alerts_when_required_failure_not_rerunnable() -> None:
    # External status context has no Actions run to re-run.
    pr = make_pr(
        mergeStateStatus="BLOCKED", statusCheckRollup=[status_context("ci", "FAILURE")]
    )
    decision = bp.classify(pr, REQUIRED, "zkoppert", {})
    assert decision.do_rerun is False
    assert bp.ALERT_CI_STILL_FAILING in decision.alerts


def test_classify_pending_required_does_nothing() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        statusCheckRollup=[check_run("ci", "", "IN_PROGRESS")],
    )
    decision = bp.classify(pr, REQUIRED, "zkoppert", {})
    assert decision.do_rerun is False
    assert decision.alerts == []


def test_classify_updates_branch_when_strict_and_behind() -> None:
    decision = bp.classify(make_pr(mergeStateStatus="BEHIND"), REQUIRED, "zkoppert", {})
    assert decision.do_update_branch is True


def test_classify_update_priority_skips_rerun() -> None:
    # A behind branch with a failing required check updates first; the
    # old-head rerun is skipped because the update creates a new head.
    pr = make_pr(
        mergeStateStatus="BEHIND", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    decision = bp.classify(pr, REQUIRED, "zkoppert", {})
    assert decision.do_update_branch is True
    assert decision.do_rerun is False
    assert bp.ALERT_CI_STILL_FAILING not in decision.alerts


def test_classify_no_update_when_not_strict_or_unknown() -> None:
    assert (
        bp.classify(
            make_pr(mergeStateStatus="BEHIND"), REQUIRED_LOOSE, "zkoppert", {}
        ).do_update_branch
        is False
    )
    assert (
        bp.classify(
            make_pr(mergeStateStatus="BEHIND"), UNKNOWN, "zkoppert", {}
        ).do_update_branch
        is False
    )


def test_classify_update_only_once_per_head() -> None:
    pr = make_pr(mergeStateStatus="BEHIND")
    assert (
        bp.classify(pr, REQUIRED, "zkoppert", {"update_head": HEAD}).do_update_branch
        is False
    )


def test_classify_conflicts_alert() -> None:
    pr = make_pr(mergeStateStatus="DIRTY", mergeable="CONFLICTING")
    assert bp.ALERT_CONFLICTS in bp.classify(pr, REQUIRED, "zkoppert", {}).alerts


def test_classify_changes_requested_alert() -> None:
    pr = make_pr(reviewDecision="CHANGES_REQUESTED", mergeStateStatus="BLOCKED")
    assert (
        bp.ALERT_CHANGES_REQUESTED in bp.classify(pr, REQUIRED, "zkoppert", {}).alerts
    )


def test_classify_new_comment_alert_then_deduped() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        comments=[
            {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}
        ],
    )
    first = bp.classify(pr, REQUIRED, "zkoppert", {})
    assert bp.ALERT_NEW_COMMENT in first.alerts
    assert first.current_activity == "2026-07-10T06:00:00Z"

    second = bp.classify(
        pr, REQUIRED, "zkoppert", {"last_activity": "2026-07-10T06:00:00Z"}
    )
    assert bp.ALERT_NEW_COMMENT not in second.alerts


def test_classify_ready_only_when_clean_and_not_draft() -> None:
    assert (
        bp.ALERT_READY
        in bp.classify(
            make_pr(mergeStateStatus="CLEAN"), REQUIRED, "zkoppert", {}
        ).alerts
    )
    draft = make_pr(mergeStateStatus="CLEAN", isDraft=True)
    assert bp.ALERT_READY not in bp.classify(draft, REQUIRED, "zkoppert", {}).alerts


# ---------------------------------------------------------------------------
# authorship gating: assigned-but-not-authored PRs are alert-only
# ---------------------------------------------------------------------------


def test_assigned_pr_does_not_auto_update_branch() -> None:
    pr = make_pr(mergeStateStatus="BEHIND", author={"login": "someone-else"})
    decision = bp.classify(pr, REQUIRED, ME, {})
    assert decision.do_update_branch is False


def test_assigned_pr_does_not_rerun_but_alerts() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        author={"login": "someone-else"},
        statusCheckRollup=[check_run("ci", "FAILURE")],
    )
    decision = bp.classify(pr, REQUIRED, ME, {})
    assert decision.do_rerun is False
    assert bp.ALERT_CI_STILL_FAILING in decision.alerts  # surfaced, not re-run


def test_assigned_pr_still_gets_human_alerts() -> None:
    pr = make_pr(
        mergeStateStatus="DIRTY", mergeable="CONFLICTING", author={"login": "x"}
    )
    assert bp.ALERT_CONFLICTS in bp.classify(pr, REQUIRED, ME, {}).alerts


# ---------------------------------------------------------------------------
# weekdays_since + nudge reviewers
# ---------------------------------------------------------------------------


def test_weekdays_since_excludes_weekends() -> None:
    # Fri 2026-07-10 -> Mon 2026-07-13 is one weekday (the weekend does not count).
    assert bp.weekdays_since("2026-07-10T00:00:00Z", NOW) == 1
    # Mon 2026-07-06 -> Mon 2026-07-13 is five weekdays.
    assert bp.weekdays_since("2026-07-06T00:00:00Z", NOW) == 5


def test_weekdays_since_unparseable_or_future_is_zero() -> None:
    assert bp.weekdays_since("not-a-date", NOW) == 0
    assert bp.weekdays_since("2026-07-20T00:00:00Z", NOW) == 0


def test_nudge_fires_for_idle_authored_pr() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED", reviewDecision="REVIEW_REQUIRED", updatedAt=OLD
    )
    decision = bp.classify(pr, REQUIRED, ME, {}, now=NOW, nudge_weekdays=3)
    assert bp.ALERT_NUDGE_REVIEWERS in decision.alerts


def test_nudge_does_not_fire_before_threshold() -> None:
    fresh = make_pr(
        mergeStateStatus="BLOCKED", updatedAt="2026-07-09T00:00:00Z"
    )  # 2 weekdays
    decision = bp.classify(fresh, REQUIRED, ME, {}, now=NOW, nudge_weekdays=3)
    assert bp.ALERT_NUDGE_REVIEWERS not in decision.alerts


def test_nudge_never_for_assigned_or_draft() -> None:
    assigned = make_pr(mergeStateStatus="BLOCKED", updatedAt=OLD, author={"login": "x"})
    assert (
        bp.ALERT_NUDGE_REVIEWERS
        not in bp.classify(assigned, REQUIRED, ME, {}, now=NOW).alerts
    )
    draft = make_pr(mergeStateStatus="BLOCKED", updatedAt=OLD, isDraft=True)
    assert (
        bp.ALERT_NUDGE_REVIEWERS
        not in bp.classify(draft, REQUIRED, ME, {}, now=NOW).alerts
    )


def test_nudge_suppressed_when_ball_is_in_my_court() -> None:
    # Conflicts, failing CI, changes requested, ready-to-merge, a scheduled
    # rerun, pending CI, or an approval all mean the reviewers are not the
    # blocker, so no nudge even when idle.
    def nudged(pr: dict[str, Any]) -> bool:
        return (
            bp.ALERT_NUDGE_REVIEWERS
            in bp.classify(pr, REQUIRED, ME, {}, now=NOW).alerts
        )

    assert not nudged(
        make_pr(mergeStateStatus="DIRTY", mergeable="CONFLICTING", updatedAt=OLD)
    )
    assert not nudged(
        make_pr(
            mergeStateStatus="BLOCKED",
            reviewDecision="CHANGES_REQUESTED",
            updatedAt=OLD,
        )
    )
    # Already approved: mine to merge, not a reviewer nudge.
    assert not nudged(
        make_pr(mergeStateStatus="BLOCKED", reviewDecision="APPROVED", updatedAt=OLD)
    )
    # Ready to merge (CLEAN) is its own alert, not a nudge.
    assert not nudged(make_pr(mergeStateStatus="CLEAN", updatedAt=OLD))
    # Failed required check still red after re-run (already reran this head).
    still_red = make_pr(
        mergeStateStatus="BLOCKED",
        updatedAt=OLD,
        statusCheckRollup=[check_run("ci", "FAILURE")],
    )
    assert (
        bp.ALERT_NUDGE_REVIEWERS
        not in bp.classify(
            still_red, REQUIRED, ME, {"rerun_head": HEAD}, now=NOW
        ).alerts
    )


def test_nudge_not_fired_while_rerun_scheduled() -> None:
    # First failed-CI run schedules a re-run without a ci-failing alert yet;
    # the nudge must still be suppressed (CI, not reviewers, is the blocker).
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        updatedAt=OLD,
        statusCheckRollup=[check_run("ci", "FAILURE")],
    )
    decision = bp.classify(pr, REQUIRED, ME, {}, now=NOW)
    assert decision.do_rerun is True
    assert bp.ALERT_NUDGE_REVIEWERS not in decision.alerts


def test_nudge_not_fired_while_required_pending() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        updatedAt=OLD,
        statusCheckRollup=[check_run("ci", "", "IN_PROGRESS")],
    )
    assert (
        bp.ALERT_NUDGE_REVIEWERS
        not in bp.classify(pr, REQUIRED, ME, {}, now=NOW).alerts
    )


def test_assigned_behind_pr_still_surfaces_failing_ci() -> None:
    # A strict+behind PR I did not author is not updated (not my branch), but
    # its failing required check must still be surfaced, not silently skipped.
    pr = make_pr(
        mergeStateStatus="BEHIND",
        author={"login": "someone-else"},
        statusCheckRollup=[check_run("ci", "FAILURE")],
    )
    decision = bp.classify(pr, REQUIRED, ME, {})
    assert decision.do_update_branch is False
    assert decision.do_rerun is False
    assert bp.ALERT_CI_STILL_FAILING in decision.alerts


def test_nudge_suppressed_when_required_unknown_and_ci_red() -> None:
    # Branch protection is unreadable (a common no-admin case), so the required
    # set is unknown and there is no ci-failing alert. The raw rollup is still
    # red, so the ball is on me to green CI, not on the reviewers: no nudge.
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        reviewDecision="REVIEW_REQUIRED",
        updatedAt=OLD,
        statusCheckRollup=[check_run("build", "FAILURE")],
    )
    decision = bp.classify(pr, UNKNOWN, ME, {}, now=NOW, nudge_weekdays=3)
    assert bp.ALERT_NUDGE_REVIEWERS not in decision.alerts


def test_nudge_fires_when_required_unknown_but_ci_green() -> None:
    # Same unreadable-protection case, but CI is green, so the fallback must not
    # over-suppress: an idle authored PR still nudges the reviewers.
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        reviewDecision="REVIEW_REQUIRED",
        updatedAt=OLD,
        statusCheckRollup=[check_run("build", "SUCCESS")],
    )
    decision = bp.classify(pr, UNKNOWN, ME, {}, now=NOW, nudge_weekdays=3)
    assert bp.ALERT_NUDGE_REVIEWERS in decision.alerts


def test_nudge_disabled_when_weekdays_zero() -> None:
    pr = make_pr(mergeStateStatus="BLOCKED", updatedAt=OLD)
    decision = bp.classify(pr, REQUIRED, ME, {}, now=NOW, nudge_weekdays=0)
    assert bp.ALERT_NUDGE_REVIEWERS not in decision.alerts


# ---------------------------------------------------------------------------
# state + lock
# ---------------------------------------------------------------------------


def test_save_state_atomic_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    bp.save_state(path, {"u": {"notified_sig": "ready-to-merge"}})
    assert bp.load_state(path) == {"u": {"notified_sig": "ready-to-merge"}}


def test_acquire_lock_is_exclusive(tmp_path: Path) -> None:
    lockpath = tmp_path / "state.lock"
    first = bp.acquire_lock(lockpath)
    assert first is not None
    assert bp.acquire_lock(lockpath) is None  # held
    first.close()
    again = bp.acquire_lock(lockpath)
    assert again is not None
    again.close()


# ---------------------------------------------------------------------------
# run() end-to-end
# ---------------------------------------------------------------------------


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


def _patch_run(pr_or_side: Any, required: bp.RequiredChecks = REQUIRED, **extra: Any):
    fetch_kwargs = (
        {"side_effect": pr_or_side}
        if isinstance(pr_or_side, list)
        else {"return_value": pr_or_side}
    )
    prs = extra.pop("prs", [("o/r", 1)])
    patches = [
        mock.patch.object(bp, "get_my_login", return_value="zkoppert"),
        mock.patch.object(bp, "search_my_open_prs", return_value=prs),
        mock.patch.object(bp, "fetch_pr", **fetch_kwargs),
        mock.patch.object(bp, "fetch_required_checks", return_value=required),
    ]
    return patches


def test_run_notifies_once_then_dedupes(tmp_path: Path) -> None:
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        bp,
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
        stats1 = bp.run(_args(tmp_path))
        stats2 = bp.run(_args(tmp_path))
        assert stats1.notified == 1
        assert stats2.notified == 0
        assert m["notify"].call_count == 1
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved[pr["url"]]["notified_sig"] == bp.ALERT_READY


def test_run_ready_flap_does_not_renotify(tmp_path: Path) -> None:
    """CLEAN -> BLOCKED -> CLEAN on the same head must notify only once.

    Regression for the persistent-alert flap re-notify bug: a green PR
    that briefly goes BLOCKED when the base moves must not re-ping.
    """
    clean = make_pr(mergeStateStatus="CLEAN")
    blocked = make_pr(mergeStateStatus="BLOCKED")
    with mock.patch.multiple(
        bp,
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
        bp.run(_args(tmp_path))
        bp.run(_args(tmp_path))
        bp.run(_args(tmp_path))
        assert m["notify"].call_count == 1


def test_run_new_comment_then_quiet(tmp_path: Path) -> None:
    pr = make_pr(
        mergeStateStatus="CLEAN",
        comments=[
            {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}
        ],
    )
    with mock.patch.multiple(
        bp,
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
        bp.run(_args(tmp_path))
        bp.run(_args(tmp_path))
        assert m["notify"].call_count == 1


def test_run_inline_reply_triggers_notification(tmp_path: Path) -> None:
    """An inline review-thread reply (no new submitted review) still pings."""
    pr = make_pr(mergeStateStatus="BLOCKED")
    replies = [{"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}]
    with mock.patch.multiple(
        bp,
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
        bp.run(_args(tmp_path))
        m["notify"].assert_called_once()
        assert "New review comment" in m["notify"].call_args.args[1]


def test_run_no_notify_does_not_consume_dedup(tmp_path: Path) -> None:
    """--no-notify must not advance the notify de-dup state, or the event
    would be permanently swallowed. Regression for the state-leak bug."""
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        bp,
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
        bp.run(_args(tmp_path, no_notify=True))
        assert m["notify"].call_count == 0
        bp.run(_args(tmp_path, no_notify=False))
        assert m["notify"].call_count == 1


def test_nudge_refires_after_activity_then_idle(tmp_path: Path) -> None:
    """nudge -> reviewer comment -> idle again must re-nudge (not one-shot).

    Regression for the finding that the persistent-signature preservation
    silenced every nudge after the first reviewer touch. Driven at the
    _process_pr layer where the de-dup lives, with an injected now.
    """
    utc = datetime.timezone.utc
    ctx = bp.RunContext(
        my_login=ME,
        dry_run=False,
        no_notify=False,
        state={},
        now=datetime.datetime(2026, 7, 13, 12, 0, tzinfo=utc),  # Mon
        nudge_weekdays=3,
    )
    ctx.required_cache["o/r@main"] = REQUIRED  # avoid a gh call
    stats = bp.BabysitStats()
    with mock.patch.object(
        bp, "notify", return_value=True
    ) as notify_mock, mock.patch.object(bp, "fetch_review_comments", return_value=[]):
        # Run 1: idle 5 weekdays -> nudge fires.
        bp._process_pr(
            make_pr(mergeStateStatus="BLOCKED", updatedAt=OLD), "o/r", 1, ctx, stats
        )
        assert notify_mock.call_count == 1
        assert (
            bp.ALERT_LABELS[bp.ALERT_NUDGE_REVIEWERS] in notify_mock.call_args.args[1]
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
        bp._process_pr(commented, "o/r", 1, ctx, stats)
        assert notify_mock.call_count == 2

        # Run 3: idle again 4 weekdays later, no further activity -> re-nudge.
        ctx.now = datetime.datetime(2026, 7, 20, 12, 0, tzinfo=utc)  # next Mon
        bp._process_pr(commented, "o/r", 1, ctx, stats)
        assert notify_mock.call_count == 3


def test_run_notify_failure_is_retried(tmp_path: Path) -> None:
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        bp,
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
        bp.run(_args(tmp_path))
        m["notify"].return_value = True  # recovers
        bp.run(_args(tmp_path))
        assert m["notify"].call_count == 2  # retried, not consumed


def test_run_update_priority_skips_rerun(tmp_path: Path) -> None:
    pr = make_pr(
        mergeStateStatus="BEHIND", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    with mock.patch.multiple(
        bp,
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
        stats = bp.run(_args(tmp_path))
        m["update_branch"].assert_called_once()
        m["rerun_runs"].assert_not_called()
        assert stats.updated == 1
        assert stats.reran == 0


def test_run_reruns_specific_runs_when_not_behind(tmp_path: Path) -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    with mock.patch.multiple(
        bp,
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
        stats = bp.run(_args(tmp_path))
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
        bp,
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
        stats = bp.run(_args(tmp_path))
        m["notify"].assert_called_once()
        assert "CI still failing" in m["notify"].call_args.args[1]
        assert stats.reran == 0
    # rerun_head advanced so the next run does not retry the doomed re-run forever
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved[pr["url"]]["rerun_head"] == HEAD
    pr = make_pr(mergeStateStatus="BEHIND")
    with mock.patch.multiple(
        bp,
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
        bp.run(_args(tmp_path))
        m["notify"].assert_called_once()
        title, subtitle, url = m["notify"].call_args.args
        assert title == "o/r#1"
        assert "Branch update failed" in subtitle
        assert url == "https://github.com/o/r/pull/1"


def test_run_notifies_per_pr_no_digest(tmp_path: Path) -> None:
    prs = [(f"o/r{i}", i) for i in range(12)]

    def fake_fetch(repo: str, number: int) -> dict[str, Any]:
        return make_pr(number=number, url=f"https://github.com/{repo}/pull/{number}")

    with mock.patch.multiple(
        bp,
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
        stats = bp.run(_args(tmp_path))
        assert m["notify"].call_count == 12
        assert stats.notified == 12
        for call in m["notify"].call_args_list:
            assert call.args[2].startswith("https://github.com/")


def test_run_isolates_per_pr_errors(tmp_path: Path) -> None:
    good = make_pr(
        number=2, url="https://github.com/o/r/pull/2", mergeStateStatus="CLEAN"
    )

    def fetch(repo: str, number: int) -> dict[str, Any]:
        if number == 1:
            raise FileNotFoundError("gh missing")
        return good

    with mock.patch.multiple(
        bp,
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
        stats = bp.run(_args(tmp_path))
        assert stats.scanned == 2
        assert stats.notified == 1  # the good PR still processed
        assert stats.errors  # the bad PR recorded


def test_run_records_error_when_login_fails(tmp_path: Path) -> None:
    with mock.patch.object(bp, "get_my_login", side_effect=LookupError("boom")):
        stats = bp.run(_args(tmp_path))
    assert stats.errors
    assert stats.scanned == 0


def test_run_records_save_error(tmp_path: Path) -> None:
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        bp,
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
        stats = bp.run(_args(tmp_path))
        assert any("save state" in e for e in stats.errors)


def test_run_skips_when_locked(tmp_path: Path) -> None:
    lock = bp.acquire_lock((tmp_path / "state.json").with_suffix(".lock"))
    assert lock is not None
    try:
        with mock.patch.object(bp, "get_my_login") as login_mock:
            stats = bp.run(_args(tmp_path))
        login_mock.assert_not_called()  # bailed before doing any work
        assert stats.scanned == 0
    finally:
        lock.close()


def test_run_dry_run_skips_state_write(tmp_path: Path) -> None:
    pr = make_pr(mergeStateStatus="CLEAN")
    with mock.patch.multiple(
        bp,
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
        bp.run(_args(tmp_path, dry_run=True))
        m["notify"].assert_called_once()  # invoked with dry_run=True for the log line
    assert not (tmp_path / "state.json").exists()


# ---------------------------------------------------------------------------
# rerun_runs
# ---------------------------------------------------------------------------


def test_rerun_runs_empty_is_noop() -> None:
    with mock.patch.object(bp, "run_gh") as gh:
        assert bp.rerun_runs("o/r", [], dry_run=False) is False
        gh.assert_not_called()


def test_rerun_runs_dry_run() -> None:
    with mock.patch.object(bp, "run_gh") as gh:
        assert bp.rerun_runs("o/r", [1], dry_run=True) is True
        gh.assert_not_called()


def test_rerun_runs_triggers_each() -> None:
    with mock.patch.object(bp, "run_gh", return_value="") as gh:
        assert bp.rerun_runs("o/r", [1, 2], dry_run=False) is True
        assert gh.call_count == 2


# ---------------------------------------------------------------------------
# gh wrappers, notify, state, argparse (coverage of the subprocess boundaries)
# ---------------------------------------------------------------------------


def test_get_my_login_success_and_error() -> None:
    with mock.patch.object(bp, "run_gh", return_value='{"login": "octocat"}'):
        assert bp.get_my_login() == "octocat"
    with mock.patch.object(bp, "run_gh", return_value='{"nope": 1}'):
        with pytest.raises(LookupError):
            bp.get_my_login()


def test_fetch_pr_success_and_error() -> None:
    with mock.patch.object(bp, "run_gh", return_value='{"number": 5}'):
        assert bp.fetch_pr("o/r", 5) == {"number": 5}
    with mock.patch.object(
        bp, "run_gh", side_effect=subprocess.CalledProcessError(1, "gh")
    ):
        assert bp.fetch_pr("o/r", 5) is None
    with mock.patch.object(bp, "run_gh", return_value="not json"):
        assert bp.fetch_pr("o/r", 5) is None


def test_fetch_required_checks_from_protection() -> None:
    payload = json.dumps(
        {"strict": True, "contexts": ["ci"], "checks": [{"context": "build"}]}
    )
    with mock.patch.object(bp, "run_gh", return_value=payload):
        rc = bp.fetch_required_checks("o/r", "main")
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

    with mock.patch.object(bp, "run_gh", side_effect=gh):
        rc = bp.fetch_required_checks("o/r", "main")
    assert rc.contexts == {"test"}
    assert rc.strict is True


def test_fetch_required_checks_unknown_when_both_fail() -> None:
    with mock.patch.object(
        bp, "run_gh", side_effect=subprocess.CalledProcessError(1, "gh")
    ):
        rc = bp.fetch_required_checks("o/r", "main")
    assert rc.known is False


def test_fetch_required_checks_no_base() -> None:
    assert bp.fetch_required_checks("o/r", "").known is False


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

    with mock.patch.object(bp, "run_gh", side_effect=gh):
        rc = bp.fetch_required_checks("o/r", "main")
    assert rc.contexts == {"lint", "test"}  # both sources unioned
    assert rc.strict is True  # strict if either source is strict


def test_update_branch_success_and_failure() -> None:
    with mock.patch.object(bp, "run_gh", return_value=""):
        assert bp.update_branch("o/r", 1, dry_run=False) is True
    with mock.patch.object(
        bp, "run_gh", side_effect=subprocess.TimeoutExpired("gh", 45)
    ):
        assert bp.update_branch("o/r", 1, dry_run=False) is False
    with mock.patch.object(bp, "run_gh") as gh:
        assert bp.update_branch("o/r", 1, dry_run=True) is True
        gh.assert_not_called()


def test_osa_str_escapes_quotes_and_backslashes() -> None:
    assert bp._osa_str('a"b\\c') == '"a\\"b\\\\c"'


def test_notify_uses_terminal_notifier_when_present() -> None:
    with mock.patch.object(
        bp.shutil, "which", return_value="/bin/terminal-notifier"
    ), mock.patch.object(bp, "_run_quiet", return_value=True) as rq:
        assert bp.notify("t", "s", "https://x/pull/1", dry_run=False) is True
    argv = rq.call_args.args[0]
    assert argv[0] == "/bin/terminal-notifier"
    assert "-open" in argv


def test_notify_falls_back_to_osascript() -> None:
    with mock.patch.object(bp.shutil, "which", return_value=None), mock.patch.object(
        bp, "_run_quiet", return_value=True
    ) as rq:
        assert bp.notify("t", "s", "https://x/pull/1", dry_run=False) is True
    assert rq.call_args.args[0][0] == "osascript"


def test_notify_dry_run_returns_false() -> None:
    with mock.patch.object(bp, "_run_quiet") as rq:
        assert bp.notify("t", "s", "u", dry_run=True) is False
        rq.assert_not_called()


def test_run_quiet_returns_bool() -> None:
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    with mock.patch.object(bp.subprocess, "run", return_value=ok):
        assert bp._run_quiet(["true"]) is True
    with mock.patch.object(bp.subprocess, "run", side_effect=FileNotFoundError):
        assert bp._run_quiet(["nope"]) is False


def test_load_state_missing_and_corrupt(tmp_path: Path) -> None:
    assert bp.load_state(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert bp.load_state(bad) == {}


def test_fetch_review_comments_paginated_pages() -> None:
    pages = json.dumps(
        [
            [{"user": {"login": "a"}, "created_at": "2026-07-10T01:00:00Z"}],
            [{"user": {"login": "b"}, "created_at": "2026-07-10T02:00:00Z"}],
        ]
    )
    with mock.patch.object(bp, "run_gh", return_value=pages):
        out = _REAL_FETCH_REVIEW_COMMENTS("o/r", 1)
    assert [c["author"]["login"] for c in out] == ["a", "b"]


def test_parse_args_owner_repeatable() -> None:
    ns = bp.parse_args(["--owner", "a", "--owner", "b", "--dry-run"])
    assert ns.owner == ["a", "b"]
    assert ns.dry_run is True


def test_parse_args_nudge_weekdays_default_and_override() -> None:
    assert bp.parse_args([]).nudge_weekdays == bp.DEFAULT_NUDGE_WEEKDAYS
    assert bp.parse_args(["--nudge-weekdays", "5"]).nudge_weekdays == 5


def test_main_returns_exit_code() -> None:
    ok = bp.BabysitStats(scanned=1)
    with mock.patch.object(bp, "run", return_value=ok):
        assert bp.main(["--dry-run"]) == 0
    err = bp.BabysitStats()
    err.errors.append("boom")
    with mock.patch.object(bp, "run", return_value=err):
        assert bp.main(["--dry-run"]) == 1
