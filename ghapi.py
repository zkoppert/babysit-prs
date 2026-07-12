"""Thin wrappers over the ``gh`` CLI: auth, PR search, and PR fetching."""

from __future__ import annotations

import datetime
import json
import subprocess
from typing import Any

from constants import logger


def run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Run ``gh <args>`` and return stdout, or raise on non-zero exit."""
    cmd = ["gh", *args]
    logger.debug("running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout


def get_my_login() -> str:
    """Return the authenticated GitHub login from ``gh api /user``."""
    out = run_gh(["api", "/user"])
    payload = json.loads(out)
    if not isinstance(payload, dict) or not payload.get("login"):
        raise LookupError(f"unexpected /user response shape: {type(payload).__name__}")
    login = payload["login"]
    if not isinstance(login, str):
        raise LookupError(f"login field is not a string: {type(login).__name__}")
    return login


def search_my_open_prs(
    owners: set[str] | None,
    allowed: set[str],
    active_days: int = 14,
    skip: set[str] | None = None,
) -> list[tuple[str, int]]:
    """Return ``(repo, number)`` for your recently-active open PRs.

    The result is the union of PRs you authored and PRs assigned to you,
    deduplicated. ``owners`` optionally limits the scan to one or more
    org/user owners; when empty or None, all such PRs are considered.
    ``active_days`` bounds the scan to PRs updated within that many days so
    the loop stays fast even when hundreds of stale PRs are open. ``skip``
    drops specific repos (for example a fixture repo whose state must not be
    mutated) even though the owner is in scope.
    """
    owners = owners or set()
    skip = skip or set()
    prs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    # Author and assignee are separate GitHub search qualifiers; combining
    # them in one query would AND (PRs both authored and assigned), so run
    # each role separately and union the results.
    for role in ("--author=@me", "--assignee=@me"):
        for repo, number in _search_role(role, owners, active_days):
            key = (repo, number)
            if key in seen or repo in skip:
                continue
            if allowed and repo not in allowed:
                continue
            seen.add(key)
            prs.append(key)
    return prs


def _search_role(
    role_flag: str, owners: set[str], active_days: int
) -> list[tuple[str, int]]:
    """Run one ``gh search prs`` role query and parse ``(repo, number)`` rows."""
    args = [
        "search",
        "prs",
        role_flag,
        "--state=open",
        "--json",
        "number,repository",
        "--limit",
        "1000",
    ]
    if active_days > 0:
        cutoff = datetime.date.today() - datetime.timedelta(days=active_days)
        args.append(f"updated:>={cutoff.isoformat()}")
    for owner in sorted(owners):
        args.append(f"--owner={owner}")
    try:
        out = run_gh(args, timeout=45)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("search (%s) failed: %s", role_flag, exc)
        return []
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        logger.warning("search (%s) parse error: %s", role_flag, exc)
        return []
    parsed: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        repo = ((row.get("repository") or {}).get("nameWithOwner") or "").strip()
        number = row.get("number")
        if repo and isinstance(number, int):
            parsed.append((repo, number))
    return parsed


PR_FIELDS = (
    "number,title,url,state,isDraft,mergeable,mergeStateStatus,"
    "headRefOid,baseRefName,reviewDecision,statusCheckRollup,"
    "latestReviews,comments,author,updatedAt"
)


def fetch_pr(repo: str, number: int) -> dict[str, Any] | None:
    """Fetch the PR fields needed for the decision tree."""
    try:
        out = run_gh(
            ["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS],
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("fetch_pr failed for %s#%d: %s", repo, number, exc)
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        logger.warning("fetch_pr: parse error for %s#%d: %s", repo, number, exc)
        return None


def fetch_review_comments(repo: str, number: int) -> list[dict[str, Any]]:
    """Return inline review-thread comments for a PR.

    ``gh pr view`` exposes submitted reviews and conversation comments but
    not inline review-thread replies, so fetch those from the REST API and
    normalize each to the ``{author, createdAt}`` shape the activity scan
    already understands. Errors return an empty list (a missed inline reply
    this run, never a crash).
    """
    try:
        out = run_gh(
            [
                "api",
                f"/repos/{repo}/pulls/{number}/comments",
                "--paginate",
                "--slurp",
            ],
            timeout=30,
        )
        pages = json.loads(out or "[]")
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning("fetch_review_comments failed for %s#%d: %s", repo, number, exc)
        return []
    comments: list[dict[str, Any]] = []
    for page in pages if isinstance(pages, list) else []:
        for item in page if isinstance(page, list) else [page]:
            if not isinstance(item, dict):
                continue
            login = (item.get("user") or {}).get("login") or ""
            when = item.get("created_at") or ""
            if login and when:
                comments.append({"author": {"login": login}, "createdAt": when})
    return comments
