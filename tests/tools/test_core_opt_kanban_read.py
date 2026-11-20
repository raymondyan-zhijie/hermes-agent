"""Regression tests for Core-2 (orchestrator kanban_schedule) and Core-3
(kanban_show heartbeat folding / slim mode, read_file wide-read hint).

Positive proof of the new behavior, on an isolated kanban board + temp files.
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-orch")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    yield kb, kbc


# ---------------------------------------------------------------------------
# Core-2: orchestrator can park a card via kanban_schedule (explicit task_id)
# ---------------------------------------------------------------------------

def test_schedule_in_core_tool_list():
    import toolsets
    assert "kanban_schedule" in toolsets._HERMES_CORE_TOOLS, \
        "kanban_schedule must ship in the core bundle so orchestrators can reach it"


def test_orchestrator_can_schedule_card(board):
    kb, kbc = board
    from tools import kanban_tools as kt
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="park-me", assignee="worker-a")
    # No HERMES_KANBAN_TASK in env: this is the orchestrator path.
    out = json.loads(kt._handle_schedule({"task_id": tid, "reason": "waiting on a timer"}))
    assert "error" not in out, f"orchestrator schedule refused: {out}"
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"


def test_orchestrator_schedule_requires_task_id(board):
    kb, kbc = board
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_schedule({"reason": "no id"}))
    assert "error" in out, "orchestrator must name an explicit task_id"


def test_goal_mode_card_cannot_be_scheduled(board):
    kb, kbc = board
    from tools import kanban_tools as kt
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="goal-card", assignee="w", goal_mode=True)
    out = json.loads(kt._handle_schedule({"task_id": tid, "reason": "park"}))
    assert "error" in out and "goal_mode" in out["error"], \
        "goal_mode protection must survive the orchestrator path"


def test_worker_can_still_schedule_own_card(board, monkeypatch):
    kb, kbc = board
    from tools import kanban_tools as kt
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="worker-card", assignee="test-orch")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = json.loads(kt._handle_schedule({"reason": "worker parking itself"}))
    assert "error" not in out, f"worker self-schedule broke: {out}"
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"


# ---------------------------------------------------------------------------
# Core-3a: kanban_show folds heartbeat noise + opt-in slim mode
# ---------------------------------------------------------------------------

def _running_card_with_heartbeats(kb, kbc, n=40):
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="noisy-run", assignee="w")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
        for i in range(n):
            assert kbd.heartbeat_worker(conn, tid, note=f"tick {i}", expected_run_id=run_id)
    return tid


def test_show_folds_heartbeats(board):
    kb, kbc = board
    from tools import kanban_tools as kt
    tid = _running_card_with_heartbeats(kb, kbc, n=40)
    out = json.loads(kt._handle_show({"task_id": tid}))
    events = out["events"]
    kinds = [e["kind"] for e in events]
    assert "heartbeat" not in kinds, "raw heartbeat rows must be folded away"
    assert "heartbeat_summary" in kinds
    summary = next(e for e in events if e["kind"] == "heartbeat_summary")
    assert summary["payload"]["count"] >= 40
    assert len(events) < 40, f"folded window should be tiny, got {len(events)} rows"
    # Non-heartbeat transitions (created/claimed) are preserved.
    assert any(k not in ("heartbeat", "heartbeat_summary") for k in kinds)
    assert "worker_context" in out, "default keeps worker_context for workers"


def test_show_slim_mode_omits_worker_context(board):
    kb, kbc = board
    from tools import kanban_tools as kt
    tid = _running_card_with_heartbeats(kb, kbc, n=5)
    out = json.loads(kt._handle_show({"task_id": tid, "include_worker_context": False}))
    assert "worker_context" not in out
    assert "task" in out and "events" in out, "structured state must remain"


def test_fold_helper_keeps_recent_transitions():
    from tools.kanban_tools import _fold_heartbeat_events

    class E:
        def __init__(self, kind, ts, note=None):
            self.kind = kind
            self.created_at = ts
            self.run_id = 1
            self.payload = {"note": note} if note else {}

    events = [E("created", 1)] + [E("heartbeat", 10 + i, f"t{i}") for i in range(50)] + [E("commented", 100)]
    rows = _fold_heartbeat_events(events, keep=30)
    kinds = [r["kind"] for r in rows]
    assert kinds.count("heartbeat_summary") == 1
    assert "created" in kinds and "commented" in kinds
    assert "heartbeat" not in kinds


# ---------------------------------------------------------------------------
# Core-3b: read_file wide-read hint for the 300+ line / sub-512KB band
# ---------------------------------------------------------------------------

def test_read_file_wide_read_hint(tmp_path):
    from tools.file_tools import read_file_tool
    p = tmp_path / "big.py"
    p.write_text("\n".join(f"line_{i} = {i}" for i in range(400)) + "\n")
    out = json.loads(read_file_tool(str(p), offset=1, limit=2000))
    assert "_hint" in out, "a wide read of a 400-line file should nudge toward targeted reads"
    assert "search_files" in out["_hint"]


def test_read_file_small_file_no_hint(tmp_path):
    from tools.file_tools import read_file_tool
    p = tmp_path / "small.py"
    p.write_text("\n".join(f"x_{i}" for i in range(50)) + "\n")
    out = json.loads(read_file_tool(str(p), offset=1, limit=2000))
    assert "_hint" not in out, "a 50-line file must not get the wide-read hint"


def test_read_file_narrow_read_no_wide_hint(tmp_path):
    from tools.file_tools import read_file_tool
    p = tmp_path / "big.py"
    p.write_text("\n".join(f"line_{i} = {i}" for i in range(400)) + "\n")
    out = json.loads(read_file_tool(str(p), offset=1, limit=100))
    # A narrow (targeted) read is exactly what we want — no wide-read nudge.
    assert "search_files" not in out.get("_hint", "")
