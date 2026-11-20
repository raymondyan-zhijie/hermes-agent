"""Regression tests for Core-1: cross-session shared billing (402) cooldown.

Proves the new behavior positively (not just absence of regression):
- record / remaining / clear round-trip on the shared ROOT state file
- an active bench is never extended by re-recording (guard feedback safety)
- expired bench returns None and is cleaned up
- fail-open on a corrupt state file
- a genuine billing failover arms the shared cooldown (_arm_rate_limit_cooldown)
- restore_primary_runtime refuses to restore into a benched primary
- the pre-call guard skips a benched primary and activates fallback (new sessions)
- P1 account isolation: Account A's 402 does not bench Account B on the same
  provider+endpoint, Account B's success does not clear A's bench, raw API keys
  never appear on disk, and Codex JWT principal survives access-token refresh
"""
import base64
import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason


@pytest.fixture
def billing_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    from agent import billing_cooldown as bc
    p = bc._state_path()
    assert str(home) in p, f"state file must be isolated under the temp home, got {p}"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        os.remove(p)
    yield bc


GEMINI = ("gemini", "https://generativelanguage.googleapis.com/v1beta")
KEY_A = "secret-key-account-ALPHA"
KEY_B = "secret-key-account-BRAVO"


def _fake_codex_jwt(account_id: str, sub: str, nonce: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload_obj = {
        "sub": sub,
        "nonce": nonce,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_record_remaining_clear_roundtrip(billing_env):
    bc = billing_env
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is None
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A)
    rem = bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A)
    assert rem is not None and 0 < rem <= bc._DEFAULT_BILLING_COOLDOWN_S
    bc.clear_billing_cooldown(*GEMINI, api_key=KEY_A)
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is None


def test_raw_api_key_never_stored_on_disk(billing_env):
    bc = billing_env
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A)
    raw = open(bc._state_path(), encoding="utf-8").read()
    assert KEY_A not in raw, "raw API key must never be persisted in billing.json"
    assert "fp:" in raw


def test_record_does_not_extend_active_bench(billing_env):
    bc = billing_env
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A)
    key = bc._route_key(*GEMINI, api_key=KEY_A)
    first = bc._load()[key]["reset_at"]
    time.sleep(0.02)
    # Re-record (as the pre-call guard would on each skipped call): must NOT push reset later.
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A)
    second = bc._load()[key]["reset_at"]
    assert second <= first + 1e-6, "re-recording must keep the earliest reset, not extend the bench"


def test_expired_bench_returns_none_and_cleans(billing_env):
    bc = billing_env
    key = bc._route_key(*GEMINI, api_key=KEY_A)
    data = {key: {"reset_at": time.time() - 5, "recorded_at": time.time() - 100}}
    with open(bc._state_path(), "w") as f:
        json.dump(data, f)
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is None
    assert key not in bc._load(), "expired key should be dropped"


def test_fail_open_on_corrupt_state(billing_env):
    bc = billing_env
    with open(bc._state_path(), "w") as f:
        f.write("{not json")
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is None
    # record must not raise on a corrupt file either
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A)


def test_provider_declared_reset_is_clamped(billing_env):
    bc = billing_env
    # A reset 30 days out must be clamped to the max, not bench the primary for a month.
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A, reset_at=time.time() + 30 * 86400)
    rem = bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A)
    assert rem is not None and rem <= bc._MAX_BILLING_COOLDOWN_S


def _make_agent(provider="gemini", base_url=GEMINI[1], fallback_model="openai/gpt-4o-mini", api_key=KEY_A):
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.context_compressor.get_model_context_length", return_value=200_000),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key=api_key, base_url=base_url, provider=provider,
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def test_billing_failover_arms_shared_cooldown(billing_env):
    bc = billing_env
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    agent = _make_agent(api_key=KEY_A)
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is None
    _arm_rate_limit_cooldown(agent, reason=FailoverReason.billing, reset_at=None)
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is not None, \
        "a billing failover must persist the shared cooldown for sibling instances"


def test_non_billing_failover_does_not_arm(billing_env):
    bc = billing_env
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    agent = _make_agent(api_key=KEY_A)
    _arm_rate_limit_cooldown(agent, reason=FailoverReason.rate_limit, reset_at=None)
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is None, \
        "only billing (402) arms the shared bench, not ordinary rate limits"


def test_restore_refuses_benched_primary(billing_env):
    bc = billing_env
    from agent.agent_runtime_helpers import restore_primary_runtime
    agent = _make_agent(api_key=KEY_A)
    # Put the agent into a fallback state with a primary snapshot.
    agent._fallback_activated = True
    agent._fallback_index = 0
    agent._primary_runtime = {
        "provider": GEMINI[0], "model": agent.model, "base_url": GEMINI[1],
        "api_key": KEY_A, "api_mode": "chat_completions",
    }
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A)
    assert restore_primary_runtime(agent) is False, \
        "must stay on fallback while the primary account is benched for billing"


def test_pre_call_guard_skips_benched_primary(billing_env, monkeypatch):
    bc = billing_env
    from agent import conversation_loop
    from agent.turn_api_call import nous_rate_limit_guard
    agent = _make_agent(api_key=KEY_A)
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A)
    # Fallback is available; stub the restart armer so we isolate the guard decision.
    monkeypatch.setattr(agent, "_try_activate_fallback", lambda reason=None: True)
    monkeypatch.setattr(conversation_loop, "_arm_fallback_restart",
                        lambda a, m, p, r: p)
    verdict = nous_rate_limit_guard(
        agent, _retry=MagicMock(), api_messages=[], messages=[], conversation_history=[],
        active_system_prompt="system", retry_count=3, compression_attempts=1, api_call_count=1)
    assert verdict.action == "break", \
        "a fresh session must skip the benched primary and switch to fallback before any 402"
    assert verdict.retry_count == 0


def test_pre_call_guard_noop_when_not_benched(billing_env, monkeypatch):
    from agent.turn_api_call import nous_rate_limit_guard
    agent = _make_agent(api_key=KEY_A)
    # No billing cooldown recorded: the guard must fall through to normal routing.
    verdict = nous_rate_limit_guard(
        agent, _retry=MagicMock(), api_messages=[], messages=[], conversation_history=[],
        active_system_prompt="system", retry_count=0, compression_attempts=0, api_call_count=0)
    assert verdict.action == "fallthrough"


# ---------------------------------------------------------------------------
# P1 account/credential isolation regressions
# ---------------------------------------------------------------------------

def test_account_isolation_402_on_a_does_not_bench_b(billing_env):
    bc = billing_env
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    from agent.turn_api_call import nous_rate_limit_guard
    from agent.agent_runtime_helpers import restore_primary_runtime

    agent_a = _make_agent(api_key=KEY_A)
    _arm_rate_limit_cooldown(agent_a, reason=FailoverReason.billing, reset_at=None)
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is not None

    # Healthy Account B on the exact same provider+endpoint must NOT see A's bench.
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_B) is None
    agent_b = _make_agent(api_key=KEY_B)
    verdict_b = nous_rate_limit_guard(
        agent_b, _retry=MagicMock(), api_messages=[], messages=[], conversation_history=[],
        active_system_prompt="system", retry_count=0, compression_attempts=0, api_call_count=0)
    assert verdict_b.action == "fallthrough", \
        "Account B on the same endpoint must not be benched by Account A's 402"

    # restore_primary_runtime for Account B must NOT be blocked by Account A's bench.
    agent_b._fallback_activated = True
    agent_b._fallback_index = 0
    agent_b._rate_limited_until = 0
    agent_b._primary_runtime = {
        "provider": GEMINI[0], "model": agent_b.model, "base_url": GEMINI[1],
        "api_key": KEY_B, "api_mode": "chat_completions",
    }
    with patch("agent.agent_runtime_helpers._primary_reset_gate_blocks", return_value=(False, None, None)), \
         patch("agent.route_binding.reinstall_primary_runtime"):
        assert restore_primary_runtime(agent_b) is True, \
            "Account B restore must not be blocked by Account A's billing cooldown"


def test_account_isolation_success_on_b_does_not_clear_a(billing_env):
    bc = billing_env
    from agent.fallback_cooldown import _arm_rate_limit_cooldown
    from agent.turn_response_check import check_api_response

    agent_a = _make_agent(api_key=KEY_A)
    _arm_rate_limit_cooldown(agent_a, reason=FailoverReason.billing, reset_at=None)
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is not None

    # Account B succeeds via the real check_api_response hook on the exact same
    # provider+endpoint: must NOT clear Account A's cooldown.
    agent_b = _make_agent(api_key=KEY_B)
    fake_resp = MagicMock()
    with patch("agent.turn_recovery.validate_response_shape", return_value=(False, None)), \
         patch("agent.turn_usage.note_response_reasoning_tokens"), \
         patch("agent.turn_response_check._derive_finish_reason", return_value="stop"), \
         patch("hermes_cli.observability.shared_metrics_harness.record_reply_finish"), \
         patch("agent.turn_response_check.record_response_usage",
               return_value=MagicMock(compression_attempts=0, rearmed=False)), \
         patch("agent.relay_llm.complete_logical_call"):
        verdict = check_api_response(
            agent_b, response=fake_resp, _retry=MagicMock(), thinking_spinner=None,
            messages=[], api_messages=[], api_kwargs={}, active_system_prompt="sys",
            conversation_history=[], finish_reason="stop", retry_count=0, max_retries=3,
            compression_attempts=0, max_compression_attempts=2, length_continue_retries=0,
            truncated_response_parts=[], truncated_tool_call_retries=0,
            current_turn_user_idx=0, api_call_count=1, api_request_id="req-b",
            api_start_time=time.time(), effective_task_id="", turn_id="t1",
            _preflight_compression_blocked=False, _last_preflight_pressure=None,
        )
    assert verdict.action == "break"
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is not None, \
        "Account B's success must never clear Account A's active billing cooldown"

    # By contrast, Account A's own successful check_api_response DOES clear A's bench.
    with patch("agent.turn_recovery.validate_response_shape", return_value=(False, None)), \
         patch("agent.turn_usage.note_response_reasoning_tokens"), \
         patch("agent.turn_response_check._derive_finish_reason", return_value="stop"), \
         patch("hermes_cli.observability.shared_metrics_harness.record_reply_finish"), \
         patch("agent.turn_response_check.record_response_usage",
               return_value=MagicMock(compression_attempts=0, rearmed=False)), \
         patch("agent.relay_llm.complete_logical_call"):
        check_api_response(
            agent_a, response=fake_resp, _retry=MagicMock(), thinking_spinner=None,
            messages=[], api_messages=[], api_kwargs={}, active_system_prompt="sys",
            conversation_history=[], finish_reason="stop", retry_count=0, max_retries=3,
            compression_attempts=0, max_compression_attempts=2, length_continue_retries=0,
            truncated_response_parts=[], truncated_tool_call_retries=0,
            current_turn_user_idx=0, api_call_count=1, api_request_id="req-a",
            api_start_time=time.time(), effective_task_id="", turn_id="t2",
            _preflight_compression_blocked=False, _last_preflight_pressure=None,
        )
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is None, \
        "Account A's own success should lift its billing cooldown"


def test_same_account_shares_across_models(billing_env):
    bc = billing_env
    # Same provider, base_url, and credential -> same key regardless of model.
    bc.record_billing_cooldown(*GEMINI, api_key=KEY_A)
    assert bc.billing_cooldown_remaining(*GEMINI, api_key=KEY_A) is not None


def test_codex_jwt_principal_survives_token_refresh(billing_env):
    bc = billing_env
    codex = ("openai-codex", "https://chatgpt.com/backend-api/codex")
    tok_v1 = _fake_codex_jwt("acct-1", "sub-1", "refresh-1")
    tok_v2 = _fake_codex_jwt("acct-1", "sub-1", "refresh-2")
    tok_other = _fake_codex_jwt("acct-2", "sub-2", "other")

    bc.record_billing_cooldown(*codex, api_key=tok_v1)
    assert bc.billing_cooldown_remaining(*codex, api_key=tok_v2) is not None, \
        "refreshed OAuth token for the same Codex principal must inherit the bench"
    assert bc.billing_cooldown_remaining(*codex, api_key=tok_other) is None, \
        "a different Codex account must stay isolated"
    raw = open(bc._state_path(), encoding="utf-8").read()
    assert tok_v1 not in raw and "codex:acct-1:sub-1" in raw
