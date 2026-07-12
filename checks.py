"""Required-status-check discovery and rollup interpretation."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from constants import FAILURE_CONCLUSIONS, FAILURE_STATES, PENDING_STATUSES, logger
from ghapi import run_gh


@dataclass
class RequiredChecks:
    """Required status checks for a base branch.

    ``contexts`` is ``None`` when the required set could not be read (no
    admin, ruleset not exposed), which callers treat as "unknown" and
    skip CI auto-actions on.
    """

    contexts: set[str] | None
    strict: bool

    @property
    def known(self) -> bool:
        """Return True when the required-check set could be read."""
        return self.contexts is not None


def fetch_required_checks(repo: str, base: str) -> RequiredChecks:
    """Read required status checks + strict flag for ``repo``'s base branch.

    GitHub can layer classic branch protection and organization rulesets on
    the same branch, so both sources are queried and their requirements are
    unioned (strict is true if either is strict). Returns an unknown
    ``RequiredChecks`` only when neither source is readable.
    """
    if not base:
        return RequiredChecks(contexts=None, strict=False)
    protection = _fetch_required_checks_from_protection(repo, base)
    rules = _fetch_required_checks_from_rules(repo, base)
    if not protection.known and not rules.known:
        return RequiredChecks(contexts=None, strict=False)
    contexts: set[str] = set()
    if protection.contexts:
        contexts |= protection.contexts
    if rules.contexts:
        contexts |= rules.contexts
    strict = bool(protection.strict or rules.strict)
    return RequiredChecks(contexts=contexts, strict=strict)


def _fetch_required_checks_from_protection(repo: str, base: str) -> RequiredChecks:
    try:
        out = run_gh(
            [
                "api",
                f"/repos/{repo}/branches/{base}/protection/required_status_checks",
            ],
            timeout=20,
        )
        data = json.loads(out)
    except subprocess.CalledProcessError as exc:
        logger.debug("no branch protection for %s@%s: %s", repo, base, exc)
        return RequiredChecks(contexts=None, strict=False)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "required checks (protection) error for %s@%s: %s", repo, base, exc
        )
        return RequiredChecks(contexts=None, strict=False)
    return RequiredChecks(
        contexts=_protection_contexts(data), strict=bool(data.get("strict"))
    )


def _protection_contexts(data: dict[str, Any]) -> set[str]:
    contexts: set[str] = set()
    for ctx in data.get("contexts") or []:
        if isinstance(ctx, str) and ctx:
            contexts.add(ctx)
    for check in data.get("checks") or []:
        if isinstance(check, dict):
            ctx = check.get("context")
            if isinstance(ctx, str) and ctx:
                contexts.add(ctx)
    return contexts


def _fetch_required_checks_from_rules(repo: str, base: str) -> RequiredChecks:
    try:
        out = run_gh(["api", f"/repos/{repo}/rules/branches/{base}"], timeout=20)
        rules = json.loads(out)
    except subprocess.CalledProcessError as exc:
        logger.debug("no rulesets for %s@%s: %s", repo, base, exc)
        return RequiredChecks(contexts=None, strict=False)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("required checks (rules) error for %s@%s: %s", repo, base, exc)
        return RequiredChecks(contexts=None, strict=False)

    contexts: set[str] = set()
    strict = False
    found = False
    if isinstance(rules, list):
        for rule in rules:
            if (
                not isinstance(rule, dict)
                or rule.get("type") != "required_status_checks"
            ):
                continue
            found = True
            params = rule.get("parameters") or {}
            strict = strict or bool(params.get("strict_required_status_checks_policy"))
            for check in params.get("required_status_checks") or []:
                if isinstance(check, dict):
                    ctx = check.get("context")
                    if isinstance(ctx, str) and ctx:
                        contexts.add(ctx)
    if not found:
        return RequiredChecks(contexts=None, strict=False)
    return RequiredChecks(contexts=contexts, strict=strict)


def check_name(entry: dict[str, Any]) -> str:
    """Return the check name (CheckRun) or context (StatusContext)."""
    return (entry.get("name") or entry.get("context") or "").strip()


def check_is_pending(entry: dict[str, Any]) -> bool:
    """Return True when a rollup check has not finished yet."""
    if entry.get("__typename") == "CheckRun":
        return (entry.get("status") or "").upper() in PENDING_STATUSES
    return (entry.get("state") or "").upper() in {"PENDING", "EXPECTED"}


def check_is_failed(entry: dict[str, Any]) -> bool:
    """Return True when a completed rollup check counts as failed."""
    if entry.get("__typename") == "CheckRun":
        if (entry.get("status") or "").upper() in PENDING_STATUSES:
            return False
        return (entry.get("conclusion") or "").upper() in FAILURE_CONCLUSIONS
    return (entry.get("state") or "").upper() in FAILURE_STATES


def _has_unfinished_or_failing_check(pr: dict[str, Any]) -> bool:
    """Return True when any visible rollup check is pending or has failed.

    Unlike ``required_check_status`` this ignores the required-check set, so it
    still sees red or in-flight CI when branch protection is unreadable. Used
    only to suppress the reviewer nudge in that blind spot, where we cannot
    tell whether an unfinished or failed check is the real blocker.
    """
    return any(
        isinstance(entry, dict) and (check_is_pending(entry) or check_is_failed(entry))
        for entry in pr.get("statusCheckRollup") or []
    )


def required_check_status(
    pr: dict[str, Any], required: RequiredChecks
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(failed_required_entries, any_required_pending)``.

    Returns the failed required rollup entries (not just names) so the
    caller can resolve each to its Actions run. When the required set is
    unknown, returns no failures and no pending so callers skip CI
    handling entirely.
    """
    if not required.known:
        return [], False
    contexts = required.contexts or set()
    failed: list[dict[str, Any]] = []
    pending = False
    for entry in pr.get("statusCheckRollup") or []:
        if not isinstance(entry, dict):
            continue
        if check_name(entry) not in contexts:
            continue
        if check_is_pending(entry):
            pending = True
        elif check_is_failed(entry):
            failed.append(entry)
    return failed, pending


def _run_id_from_details_url(url: str) -> int | None:
    """Extract the Actions run id from a CheckRun ``detailsUrl``."""
    match = re.search(r"/actions/runs/(\d+)", url or "")
    return int(match.group(1)) if match else None


def rerunnable_run_ids(entries: list[dict[str, Any]]) -> list[int]:
    """Return the distinct Actions run ids backing failed CheckRun entries.

    External ``StatusContext`` checks (and CheckRuns whose detailsUrl has
    no run id) are skipped: they are not Actions runs and cannot be
    re-run, so the caller surfaces them for a human instead.
    """
    ids: list[int] = []
    for entry in entries:
        if entry.get("__typename") != "CheckRun":
            continue
        run_id = _run_id_from_details_url(entry.get("detailsUrl") or "")
        if run_id is not None and run_id not in ids:
            ids.append(run_id)
    return ids
