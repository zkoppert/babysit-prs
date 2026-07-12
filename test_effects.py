# pylint: disable=missing-function-docstring,protected-access

"""Tests for effects: mutations, notifications, and state I/O."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import effects


def test_save_state_atomic_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    effects.save_state(path, {"u": {"notified_sig": "ready-to-merge"}})
    assert effects.load_state(path) == {"u": {"notified_sig": "ready-to-merge"}}


def test_acquire_lock_is_exclusive(tmp_path: Path) -> None:
    lockpath = tmp_path / "state.lock"
    first = effects.acquire_lock(lockpath)
    assert first is not None
    assert effects.acquire_lock(lockpath) is None  # held
    first.close()
    again = effects.acquire_lock(lockpath)
    assert again is not None
    again.close()


def test_rerun_runs_empty_is_noop() -> None:
    with mock.patch.object(effects, "run_gh") as gh:
        assert effects.rerun_runs("o/r", [], dry_run=False) is False
        gh.assert_not_called()


def test_rerun_runs_dry_run() -> None:
    with mock.patch.object(effects, "run_gh") as gh:
        assert effects.rerun_runs("o/r", [1], dry_run=True) is True
        gh.assert_not_called()


def test_rerun_runs_triggers_each() -> None:
    with mock.patch.object(effects, "run_gh", return_value="") as gh:
        assert effects.rerun_runs("o/r", [1, 2], dry_run=False) is True
        assert gh.call_count == 2


def test_update_branch_success_and_failure() -> None:
    with mock.patch.object(effects, "run_gh", return_value=""):
        assert effects.update_branch("o/r", 1, dry_run=False) is True
    with mock.patch.object(
        effects, "run_gh", side_effect=subprocess.TimeoutExpired("gh", 45)
    ):
        assert effects.update_branch("o/r", 1, dry_run=False) is False
    with mock.patch.object(effects, "run_gh") as gh:
        assert effects.update_branch("o/r", 1, dry_run=True) is True
        gh.assert_not_called()


def test_osa_str_escapes_quotes_and_backslashes() -> None:
    assert effects._osa_str('a"b\\c') == '"a\\"b\\\\c"'


def test_notify_uses_terminal_notifier_when_present() -> None:
    with mock.patch.object(
        effects.shutil, "which", return_value="/bin/terminal-notifier"
    ), mock.patch.object(effects, "_run_quiet", return_value=True) as rq:
        assert effects.notify("t", "s", "https://x/pull/1", dry_run=False) is True
    argv = rq.call_args.args[0]
    assert argv[0] == "/bin/terminal-notifier"
    assert "-open" in argv


def test_notify_falls_back_to_osascript() -> None:
    with mock.patch.object(
        effects.shutil, "which", return_value=None
    ), mock.patch.object(effects, "_run_quiet", return_value=True) as rq:
        assert effects.notify("t", "s", "https://x/pull/1", dry_run=False) is True
    assert rq.call_args.args[0][0] == "osascript"


def test_notify_dry_run_returns_false() -> None:
    with mock.patch.object(effects, "_run_quiet") as rq:
        assert effects.notify("t", "s", "u", dry_run=True) is False
        rq.assert_not_called()


def test_run_quiet_returns_bool() -> None:
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    with mock.patch.object(effects.subprocess, "run", return_value=ok):
        assert effects._run_quiet(["true"]) is True
    with mock.patch.object(effects.subprocess, "run", side_effect=FileNotFoundError):
        assert effects._run_quiet(["nope"]) is False


def test_load_state_missing_and_corrupt(tmp_path: Path) -> None:
    assert effects.load_state(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert effects.load_state(bad) == {}
