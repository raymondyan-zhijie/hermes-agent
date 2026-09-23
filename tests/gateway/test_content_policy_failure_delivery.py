"""A terminal content-policy block still reaches the chat with its curated copy.

2026-09-23 qualitative check (theme "CU"): the claim that a safety-filter refusal leaves the
user with ZERO signal has to survive the gateway's own shaping layer, not just the agent's.
``_content_policy_blocked_result`` returns ``final_response = "⚠️ " + content_policy_copy(...)``
and ``_normalize_empty_agent_response`` passes a NON-EMPTY response straight through even on a
failed turn (only raw provider envelopes on overflow failures are rewritten), so the curated
copy is what gets delivered. This pins that contract so a future rewrite of the failed-turn
handler cannot silently swallow it.
"""
from __future__ import annotations

from agent.conversation_loop import _content_policy_blocked_result
from agent.turn_failure_copy import content_policy_copy
from gateway.run import _normalize_empty_agent_response


def test_terminal_content_policy_copy_survives_normalization():
    detail = "HTTP 400: Content Exists Risk"
    result = _content_policy_blocked_result(
        [], 1,
        final_response="⚠️ " + content_policy_copy(label="DeepSeek", summary=detail),
        error_detail=detail,
    )

    assert result["failed"] is True
    shaped = _normalize_empty_agent_response(result, result["final_response"], history_len=3)

    assert "safety filter refused" in shaped, "curated copy must not be replaced by generic text"
    assert "Content Exists Risk" in shaped
    assert "Something went wrong" not in shaped


def test_short_history_content_policy_block_is_not_an_overflow_failure():
    from gateway.run import is_context_overflow_failure_result

    result = _content_policy_blocked_result(
        [], 1, final_response="⚠️ x", error_detail="HTTP 400: Content Exists Risk")

    assert is_context_overflow_failure_result(result, history_len=3) is False


def test_long_history_400_heuristic_also_covers_content_policy_but_copy_still_wins():
    """Documented quirk: the generic "400 on a long session => overflow" heuristic fires for a
    content-policy block too, so the copy only survives because it is NOT a raw provider
    envelope (the overflow branch rewrites envelopes, see ``_looks_like_gateway_provider_error``).
    Pinned so a future change to either side is noticed."""
    from gateway.run import is_context_overflow_failure_result

    detail = "HTTP 400: Content Exists Risk"
    result = _content_policy_blocked_result(
        [], 1, final_response="⚠️ " + content_policy_copy(label="DeepSeek", summary=detail),
        error_detail=detail)

    assert is_context_overflow_failure_result(result, history_len=80) is True
    shaped = _normalize_empty_agent_response(result, result["final_response"], history_len=80)
    assert "safety filter refused" in shaped
    assert "/compact" not in shaped
