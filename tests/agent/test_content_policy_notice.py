"""Content-policy (safety-filter) blocks must leave a user-visible notice AND an audit trail.

A provider safety-filter refusal is terminal on the first attempt (``retryable=False``,
credentials unchanged), and the retry buffer is empty on that path -- so the status flush at
turn end used to discard the pending one-shot notice, and the turn carried no record at all.
Two sinks are pinned here: the notice channel that is proven to reach the chat, and the
profile-scoped ``logs/content-policy-events.jsonl`` audit line.

Ported (2026-09-23) from the authoring copy at
``work/cu-contentpolicy-20260923/test_cu_content_policy_notice.py``: that file lived in the
work directory, so the repo had ZERO coverage of these symbols.
"""
from __future__ import annotations

import json
import pathlib

from run_agent import AIAgent


def _bare():
    """Same construction as tests/agent/test_retry_status_buffer.py: only the pure helpers."""
    agent = object.__new__(AIAgent)
    agent.log_prefix = ""
    agent.status_callback = None
    agent.suppress_output = False
    agent.suppress_status_output = False
    agent._mute_post_response = False
    agent._executing_tools = False
    agent._print_fn = None
    return agent


def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path, raising=False)


# ── sink 1: the one-shot notice must survive a terminal failure with an EMPTY retry buffer ──
def test_flush_emits_pending_notice_when_nothing_was_buffered():
    agent = _bare()
    seen = []
    agent._emit_status = lambda msg: seen.append(msg)
    agent._pending_fallback_notice = "⚠️ Content policy blocked: deepseek/m1 refused this prompt"

    agent._flush_status_buffer()

    assert seen == ["⚠️ Content policy blocked: deepseek/m1 refused this prompt"], (
        "an empty retry buffer means the trace carries nothing: dropping the notice here is "
        "what left the user with zero signal")
    assert agent._pending_fallback_notice is None


def test_flush_with_a_retry_trace_keeps_the_old_behaviour():
    """Counter-case: the buffered trace already carries the switch line -- no duplicate."""
    agent = _bare()
    seen = []
    agent._emit_status = lambda msg: seen.append(msg)
    agent._buffer_status("🔄 Primary model failed — switching to fallback: m2 via p2")
    agent._pending_fallback_notice = "🔄 Switched to fallback model: m1 via p1 → m2 via p2"

    agent._flush_status_buffer()

    assert seen == ["🔄 Primary model failed — switching to fallback: m2 via p2"]
    assert agent._pending_fallback_notice is None


# ── sink 2: the terminal content-policy result notifies the user and writes the audit line ──
def test_content_policy_result_notifies_the_user_and_records_the_block(tmp_path, monkeypatch):
    _hermetic(tmp_path, monkeypatch)
    from agent.conversation_loop import _content_policy_blocked_result

    agent = _bare()
    agent.model = "deepseek-v4-flash"
    agent.provider = "deepseek"
    agent.session_id = "sess-1"

    res = _content_policy_blocked_result(
        [], 1, final_response="⚠️ blocked", error_detail="Content Exists Risk",
        agent=agent, source="unit-test")

    notice = agent._pending_fallback_notice
    assert notice and "Content policy blocked" in str(notice)
    assert "deepseek" in str(notice) and "Content Exists Risk" in str(notice)

    path = pathlib.Path(tmp_path) / "logs" / "content-policy-events.jsonl"
    assert path.exists(), "a content-policy terminal must leave an auditable record"
    rec = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["event"] == "content_policy_blocked"
    assert rec["provider"] == "deepseek" and rec["model"] == "deepseek-v4-flash"
    assert rec["detail"] == "Content Exists Risk" and rec["session_id"] == "sess-1"
    assert rec["source"] == "unit-test"
    assert res["failure_reason"] == "content_policy_blocked" and res["failed"] is True


def test_result_without_an_agent_stays_silent(tmp_path, monkeypatch):
    """Counter-case: callers that pass no agent (older paths) keep the previous behaviour."""
    _hermetic(tmp_path, monkeypatch)
    from agent.conversation_loop import _content_policy_blocked_result

    res = _content_policy_blocked_result(
        [], 1, final_response="⚠️ blocked", error_detail="d")

    assert res["failed"] is True and res["failure_reason"] == "content_policy_blocked"
    assert not (pathlib.Path(tmp_path) / "logs" / "content-policy-events.jsonl").exists()


def test_note_never_raises_on_a_hostile_agent(tmp_path, monkeypatch):
    """Observability must never break the failure path."""
    _hermetic(tmp_path, monkeypatch)
    from agent.conversation_loop import _note_content_policy_blocked

    class Hostile:
        model = "m"
        provider = "p"

        def __getattr__(self, item):
            raise RuntimeError("hostile")

    _note_content_policy_blocked(Hostile(), "boom", source="unit-test")  # must not raise
