#!/usr/bin/env python3
"""Babysit your own open pull requests so you stop hand-watching CI.

For each of your open PRs (optionally limited to one or more owners), this
tool:

- Re-runs failed **required** checks once per head commit (a flaky-test
  retry), and only notifies you if they are still red after that retry.
- Updates the branch when the base requires up-to-date branches and the
  PR is cleanly behind (``mergeStateStatus == BEHIND``), so CI re-runs
  against the latest base without your involvement.
- Notifies you on macOS when something needs a human: merge conflicts,
  changes requested, a new human review comment, a failed branch update,
  or a PR that is green and ready to merge.

Design goals:

- Auto-act with guardrails: only touch **required** checks and only
  update-branch when the base is strict and the branch is cleanly behind.
  Anything ambiguous (unknown required set, conflicts) is left for you.
- Quiet by default: a per-PR state signature means you are pinged only
  when a PR's notable state actually changes, not every run.
- Dry-run friendly: ``--dry-run`` skips every mutation and notification.
- Pure stdlib: no third-party runtime dependencies.

Requires the ``gh`` CLI authenticated with ``repo`` and ``workflow``
scopes. macOS is required for notifications (``terminal-notifier`` makes
them clickable; otherwise it falls back to ``osascript``).

Usage:
    babysit_prs.py [--owner OWNER] [--active-days N] [--allowed-repo OWNER/REPO]
                   [--skip-repo OWNER/REPO] [--dry-run] [--no-notify]
                   [--state-file PATH] [--verbose]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from constants import DEFAULT_NUDGE_WEEKDAYS, DEFAULT_STATE_FILE
from runner import run


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Babysit your own open PRs: re-run failed required checks, update "
            "cleanly-behind branches, and notify you on macOS when something "
            "needs a human."
        ),
    )
    parser.add_argument(
        "--owner",
        action="append",
        default=[],
        metavar="OWNER",
        help=(
            "Limit to PRs in this org/user owner. Pass multiple times for "
            "multiple owners. Default: all of your open PRs."
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Per-PR notify/de-dup state file (default: {DEFAULT_STATE_FILE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview decisions; do not re-run checks, update branches, or notify.",
    )
    parser.add_argument(
        "--active-days",
        type=int,
        default=14,
        metavar="N",
        help=(
            "Only watch PRs updated within the last N days (0 = no limit). "
            "Widened automatically when --nudge-weekdays needs a longer window, "
            "so the nudge is never silently unreachable. Default: 14."
        ),
    )
    parser.add_argument(
        "--nudge-weekdays",
        type=int,
        default=DEFAULT_NUDGE_WEEKDAYS,
        metavar="N",
        help=(
            "Nudge reviewers on an authored PR idle this many weekdays "
            "(0 disables). Default: 3."
        ),
    )
    parser.add_argument(
        "--allowed-repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Process only the given repo. Pass multiple times for multiple repos.",
    )
    parser.add_argument(
        "--skip-repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Never act on the given repo (for example a fixture). Repeatable.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Do everything except send macOS notifications.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    stats = run(args)
    print(
        f"scanned={stats.scanned} reran={stats.reran} "
        f"updated={stats.updated} notified={stats.notified}"
    )
    for err in stats.errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    sys.exit(main())
