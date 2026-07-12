# pylint: disable=missing-function-docstring,protected-access

"""Tests for classify: the pure PR decision tree."""

from __future__ import annotations

from typing import Any

from prfixtures import (
    HEAD,
    ME,
    NOW,
    OLD,
    REQUIRED,
    REQUIRED_LOOSE,
    UNKNOWN,
    check_run,
    make_pr,
    status_context,
)

import classify
import constants


def test_classify_reruns_failed_required_once_then_alerts() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    first = classify.classify(pr, REQUIRED, "zkoppert", {})
    assert first.do_rerun is True
    assert first.rerun_run_ids == [999]
    assert constants.ALERT_CI_STILL_FAILING not in first.alerts

    second = classify.classify(pr, REQUIRED, "zkoppert", {"rerun_head": HEAD})
    assert second.do_rerun is False
    assert constants.ALERT_CI_STILL_FAILING in second.alerts


def test_classify_alerts_when_required_failure_not_rerunnable() -> None:
    # External status context has no Actions run to re-run.
    pr = make_pr(
        mergeStateStatus="BLOCKED", statusCheckRollup=[status_context("ci", "FAILURE")]
    )
    decision = classify.classify(pr, REQUIRED, "zkoppert", {})
    assert decision.do_rerun is False
    assert constants.ALERT_CI_STILL_FAILING in decision.alerts


def test_classify_pending_required_does_nothing() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        statusCheckRollup=[check_run("ci", "", "IN_PROGRESS")],
    )
    decision = classify.classify(pr, REQUIRED, "zkoppert", {})
    assert decision.do_rerun is False
    assert decision.alerts == []


def test_classify_updates_branch_when_strict_and_behind() -> None:
    decision = classify.classify(
        make_pr(mergeStateStatus="BEHIND"), REQUIRED, "zkoppert", {}
    )
    assert decision.do_update_branch is True


def test_classify_update_priority_skips_rerun() -> None:
    # A behind branch with a failing required check updates first; the
    # old-head rerun is skipped because the update creates a new head.
    pr = make_pr(
        mergeStateStatus="BEHIND", statusCheckRollup=[check_run("ci", "FAILURE")]
    )
    decision = classify.classify(pr, REQUIRED, "zkoppert", {})
    assert decision.do_update_branch is True
    assert decision.do_rerun is False
    assert constants.ALERT_CI_STILL_FAILING not in decision.alerts


def test_classify_no_update_when_not_strict_or_unknown() -> None:
    assert (
        classify.classify(
            make_pr(mergeStateStatus="BEHIND"), REQUIRED_LOOSE, "zkoppert", {}
        ).do_update_branch
        is False
    )
    assert (
        classify.classify(
            make_pr(mergeStateStatus="BEHIND"), UNKNOWN, "zkoppert", {}
        ).do_update_branch
        is False
    )


def test_classify_update_only_once_per_head() -> None:
    pr = make_pr(mergeStateStatus="BEHIND")
    assert (
        classify.classify(
            pr, REQUIRED, "zkoppert", {"update_head": HEAD}
        ).do_update_branch
        is False
    )


def test_classify_conflicts_alert() -> None:
    pr = make_pr(mergeStateStatus="DIRTY", mergeable="CONFLICTING")
    assert (
        constants.ALERT_CONFLICTS
        in classify.classify(pr, REQUIRED, "zkoppert", {}).alerts
    )


def test_classify_changes_requested_alert() -> None:
    pr = make_pr(reviewDecision="CHANGES_REQUESTED", mergeStateStatus="BLOCKED")
    assert (
        constants.ALERT_CHANGES_REQUESTED
        in classify.classify(pr, REQUIRED, "zkoppert", {}).alerts
    )


def test_classify_new_comment_alert_then_deduped() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        comments=[
            {"author": {"login": "octocat"}, "createdAt": "2026-07-10T06:00:00Z"}
        ],
    )
    first = classify.classify(pr, REQUIRED, "zkoppert", {})
    assert constants.ALERT_NEW_COMMENT in first.alerts
    assert first.current_activity == "2026-07-10T06:00:00Z"

    second = classify.classify(
        pr, REQUIRED, "zkoppert", {"last_activity": "2026-07-10T06:00:00Z"}
    )
    assert constants.ALERT_NEW_COMMENT not in second.alerts


def test_classify_ready_only_when_clean_and_not_draft() -> None:
    assert (
        constants.ALERT_READY
        in classify.classify(
            make_pr(mergeStateStatus="CLEAN"), REQUIRED, "zkoppert", {}
        ).alerts
    )
    draft = make_pr(mergeStateStatus="CLEAN", isDraft=True)
    assert (
        constants.ALERT_READY
        not in classify.classify(draft, REQUIRED, "zkoppert", {}).alerts
    )


# ---------------------------------------------------------------------------
# authorship gating: assigned-but-not-authored PRs are alert-only
# ---------------------------------------------------------------------------


def test_assigned_pr_does_not_auto_update_branch() -> None:
    pr = make_pr(mergeStateStatus="BEHIND", author={"login": "someone-else"})
    decision = classify.classify(pr, REQUIRED, ME, {})
    assert decision.do_update_branch is False


def test_assigned_pr_does_not_rerun_but_alerts() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        author={"login": "someone-else"},
        statusCheckRollup=[check_run("ci", "FAILURE")],
    )
    decision = classify.classify(pr, REQUIRED, ME, {})
    assert decision.do_rerun is False
    assert constants.ALERT_CI_STILL_FAILING in decision.alerts  # surfaced, not re-run


def test_assigned_pr_still_gets_human_alerts() -> None:
    pr = make_pr(
        mergeStateStatus="DIRTY", mergeable="CONFLICTING", author={"login": "x"}
    )
    assert constants.ALERT_CONFLICTS in classify.classify(pr, REQUIRED, ME, {}).alerts


def test_nudge_fires_for_idle_authored_pr() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED", reviewDecision="REVIEW_REQUIRED", updatedAt=OLD
    )
    decision = classify.classify(pr, REQUIRED, ME, {}, now=NOW, nudge_weekdays=3)
    assert constants.ALERT_NUDGE_REVIEWERS in decision.alerts


def test_nudge_does_not_fire_before_threshold() -> None:
    fresh = make_pr(
        mergeStateStatus="BLOCKED", updatedAt="2026-07-09T00:00:00Z"
    )  # 2 weekdays
    decision = classify.classify(fresh, REQUIRED, ME, {}, now=NOW, nudge_weekdays=3)
    assert constants.ALERT_NUDGE_REVIEWERS not in decision.alerts


def test_nudge_never_for_assigned_or_draft() -> None:
    assigned = make_pr(mergeStateStatus="BLOCKED", updatedAt=OLD, author={"login": "x"})
    assert (
        constants.ALERT_NUDGE_REVIEWERS
        not in classify.classify(assigned, REQUIRED, ME, {}, now=NOW).alerts
    )
    draft = make_pr(mergeStateStatus="BLOCKED", updatedAt=OLD, isDraft=True)
    assert (
        constants.ALERT_NUDGE_REVIEWERS
        not in classify.classify(draft, REQUIRED, ME, {}, now=NOW).alerts
    )


def test_nudge_suppressed_when_ball_is_in_my_court() -> None:
    # Conflicts, failing CI, changes requested, ready-to-merge, a scheduled
    # rerun, pending CI, or an approval all mean the reviewers are not the
    # blocker, so no nudge even when idle.
    def nudged(pr: dict[str, Any]) -> bool:
        return (
            constants.ALERT_NUDGE_REVIEWERS
            in classify.classify(pr, REQUIRED, ME, {}, now=NOW).alerts
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
        constants.ALERT_NUDGE_REVIEWERS
        not in classify.classify(
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
    decision = classify.classify(pr, REQUIRED, ME, {}, now=NOW)
    assert decision.do_rerun is True
    assert constants.ALERT_NUDGE_REVIEWERS not in decision.alerts


def test_nudge_not_fired_while_required_pending() -> None:
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        updatedAt=OLD,
        statusCheckRollup=[check_run("ci", "", "IN_PROGRESS")],
    )
    assert (
        constants.ALERT_NUDGE_REVIEWERS
        not in classify.classify(pr, REQUIRED, ME, {}, now=NOW).alerts
    )


def test_assigned_behind_pr_still_surfaces_failing_ci() -> None:
    # A strict+behind PR I did not author is not updated (not my branch), but
    # its failing required check must still be surfaced, not silently skipped.
    pr = make_pr(
        mergeStateStatus="BEHIND",
        author={"login": "someone-else"},
        statusCheckRollup=[check_run("ci", "FAILURE")],
    )
    decision = classify.classify(pr, REQUIRED, ME, {})
    assert decision.do_update_branch is False
    assert decision.do_rerun is False
    assert constants.ALERT_CI_STILL_FAILING in decision.alerts


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
    decision = classify.classify(pr, UNKNOWN, ME, {}, now=NOW, nudge_weekdays=3)
    assert constants.ALERT_NUDGE_REVIEWERS not in decision.alerts


def test_nudge_fires_when_required_unknown_but_ci_green() -> None:
    # Same unreadable-protection case, but CI is green, so the fallback must not
    # over-suppress: an idle authored PR still nudges the reviewers.
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        reviewDecision="REVIEW_REQUIRED",
        updatedAt=OLD,
        statusCheckRollup=[check_run("build", "SUCCESS")],
    )
    decision = classify.classify(pr, UNKNOWN, ME, {}, now=NOW, nudge_weekdays=3)
    assert constants.ALERT_NUDGE_REVIEWERS in decision.alerts


def test_nudge_suppressed_when_required_unknown_and_ci_pending() -> None:
    # With protection unreadable we cannot tell an unfinished check is required,
    # so an in-flight check keeps the ball off the reviewers: no nudge.
    pr = make_pr(
        mergeStateStatus="BLOCKED",
        reviewDecision="REVIEW_REQUIRED",
        updatedAt=OLD,
        statusCheckRollup=[check_run("build", "", "IN_PROGRESS")],
    )
    decision = classify.classify(pr, UNKNOWN, ME, {}, now=NOW, nudge_weekdays=3)
    assert constants.ALERT_NUDGE_REVIEWERS not in decision.alerts


def test_nudge_disabled_when_weekdays_zero() -> None:
    pr = make_pr(mergeStateStatus="BLOCKED", updatedAt=OLD)
    decision = classify.classify(pr, REQUIRED, ME, {}, now=NOW, nudge_weekdays=0)
    assert constants.ALERT_NUDGE_REVIEWERS not in decision.alerts
