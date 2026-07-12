"""Side effects: mutate GitHub (rerun/update), notify macOS, persist state."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from constants import logger
from ghapi import run_gh


def rerun_runs(repo: str, run_ids: list[int], *, dry_run: bool) -> bool:
    """Re-run the failed jobs of the given Actions runs.

    Returns True when at least one re-run was triggered (or in dry-run),
    so the caller only records ``rerun_head`` when a re-run actually
    happened rather than for a re-run that never occurred.
    """
    if not run_ids:
        return False
    if dry_run:
        logger.info(
            "[dry-run] would re-run %d run(s) for %s: %s", len(run_ids), repo, run_ids
        )
        return True
    triggered = False
    for run_id in run_ids:
        try:
            run_gh(
                ["run", "rerun", str(run_id), "--repo", repo, "--failed"], timeout=30
            )
            logger.info("re-ran failed jobs in %s run %s", repo, run_id)
            triggered = True
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            logger.warning("rerun failed for %s run %s: %s", repo, run_id, exc)
    return triggered


def update_branch(repo: str, number: int, *, dry_run: bool) -> bool:
    """Update the PR branch from base. Returns True on success."""
    if dry_run:
        logger.info("[dry-run] would update branch for %s#%d", repo, number)
        return True
    try:
        run_gh(["pr", "update-branch", str(number), "--repo", repo], timeout=45)
        logger.info("updated branch for %s#%d", repo, number)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("update-branch failed for %s#%d: %s", repo, number, exc)
        return False


def _osa_str(value: str) -> str:
    """Quote a Python string as an AppleScript string literal.

    Escapes only backslash and double-quote and passes raw Unicode, since
    AppleScript string literals reject the ``\\uXXXX`` escapes that
    ``json.dumps`` would emit for accented or emoji characters.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, subtitle: str, url: str, *, dry_run: bool) -> bool:
    """Send a macOS notification for one PR. Returns True on delivery.

    Uses ``terminal-notifier`` when present so the whole notification is
    clickable and opens the PR. Otherwise falls back to ``osascript``,
    which cannot open a URL on click, so the PR url is placed in the body
    to stay copyable. The delivery result gates de-dup: a failed
    notification is not recorded, so it is retried next run.
    """
    if dry_run:
        logger.info("[dry-run] notify: %s | %s | %s", title, subtitle, url)
        return False
    tn = shutil.which("terminal-notifier")
    if tn:
        args = [tn, "-title", title, "-subtitle", subtitle, "-sound", "default"]
        args += ["-message", url, "-open", url] if url else ["-message", subtitle]
        return _run_quiet(args)
    parts = [
        f"display notification {_osa_str(url or subtitle)}",
        f"with title {_osa_str(title)}",
    ]
    if subtitle:
        parts.append(f"subtitle {_osa_str(subtitle)}")
    parts.append('sound name "default"')
    return _run_quiet(["osascript", "-e", " ".join(parts)])


def _run_quiet(args: list[str]) -> bool:
    """Run a notification command, returning True on a zero exit."""
    try:
        result = subprocess.run(args, check=False, capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("notify command failed: %s", exc)
        return False
    if result.returncode != 0:
        logger.debug("notify command exited %s: %s", result.returncode, result.stderr)
    return result.returncode == 0


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """Load the per-PR state map, returning {} when absent or unreadable."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("load_state failed for %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    """Write the state file atomically. Raises OSError on failure.

    The write goes to a temp file in the same directory and is renamed
    into place, so a crashed or overlapping run can never leave a
    half-written (unparseable) state file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".babysit-state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def acquire_lock(path: Path) -> Any:
    """Take a non-blocking exclusive lock, or return None if held.

    Overlapping runs (a slow tick, or a manual invocation on top of the
    launchd schedule) would otherwise race the read-modify-write of the
    state file. The loser simply skips this tick.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")  # pylint: disable=consider-using-with
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
