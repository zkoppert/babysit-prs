"""Shared pytest config: stub the per-PR REST call runner makes."""

import pytest

import runner


@pytest.fixture(autouse=True)
def _stub_review_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep run() tests from making a real REST call per PR; tests that
    # exercise inline replies override this within the test.
    monkeypatch.setattr(runner, "fetch_review_comments", lambda repo, number: [])
