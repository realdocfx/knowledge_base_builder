"""End-to-end control flow of ``kb-builder pull``.

``pull`` is the primary acquisition command and had **no test at all**. It also
had a one-line defect that made it fail every single time: ``datetime.now()`` was
called at the end of the happy path while ``datetime`` was never imported, so a
completed sync raised ``NameError``, the outer handler swallowed it, printed
"Critical Sync Failure" and exited 1. Work was done and then reported as failure.

pyflakes would have caught it; no CI ran pyflakes, and no test ran ``pull``.
These tests assert the observable contract instead of the internals: a successful
sync exits 0 and records ``last_sync``, a failing one exits non-zero.

The network and per-item transfer are stubbed -- the defect was in the command's
control flow, which is what is under test here.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from knowledge_base_builder.cli import app

runner = CliRunner()


def _fake_items(n: int = 2):
    return [
        {"identifier": f"item_{i}", "title": f"Item {i}", "size": 1024, "files": []}
        for i in range(n)
    ]


def _run_pull(target, *, items=2, process=(1024, False)):
    """Invoke `pull` with the network and per-item transfer stubbed out."""
    engine = MagicMock()
    engine.search.return_value = iter(_fake_items(items))

    with patch("knowledge_base_builder.cli.get_engine", return_value=engine), patch(
        "knowledge_base_builder.cli._process_item", return_value=process
    ), patch("knowledge_base_builder.cli._print_report"):
        return runner.invoke(app, ["pull", "ia", "test query", str(target)])


def test_pull_exits_zero_on_success(tmp_path):
    """A completed sync must report success, not 'Critical Sync Failure'."""
    result = _run_pull(tmp_path)
    assert result.exit_code == 0, (
        "pull exited non-zero after completing its work.\n"
        f"output:\n{result.output}\n"
        f"exception: {result.exception!r}"
    )
    assert "Critical Sync Failure" not in result.output


def test_pull_records_last_sync_timestamp(tmp_path):
    """The state file must carry a parseable last_sync after a successful pull.

    This is the specific line that raised NameError, so it doubles as the
    regression guard for the missing import.
    """
    from datetime import datetime as _dt

    result = _run_pull(tmp_path)
    assert result.exit_code == 0, result.output

    state = json.loads((tmp_path / ".kb_state" / "sync_state.json").read_text(encoding="utf-8"))
    assert state.get("last_sync"), f"last_sync not recorded; state={state}"
    # Must be a real ISO-8601 timestamp, not a placeholder or a repr.
    _dt.fromisoformat(state["last_sync"])


def test_pull_surfaces_engine_failure_as_nonzero_exit(tmp_path):
    """A genuine failure must still exit non-zero -- don't fix D1 by swallowing."""
    engine = MagicMock()
    engine.search.side_effect = RuntimeError("backend unreachable")

    with patch("knowledge_base_builder.cli.get_engine", return_value=engine), patch(
        "knowledge_base_builder.cli._print_report"
    ):
        result = runner.invoke(app, ["pull", "ia", "q", str(tmp_path)])

    assert result.exit_code != 0
