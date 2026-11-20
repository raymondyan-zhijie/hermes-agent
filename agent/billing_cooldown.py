"""Cross-session / cross-profile billing (HTTP 402) cooldown for the primary route.

The per-agent ``_rate_limited_until`` cooldown is instance-local: a fresh gateway
session, cron run or kanban worker starts with a clean slate and re-tries a primary
that is already out of credit, burning a 402 + a cache-invalidating fallback on every
new instance (observed 2026-11-19: 95 Gemini 402s across profiles/workers). The
credential pool only helps providers that HAVE a pool; a single env-key route does not.

This module persists a short billing cooldown keyed by account-scoped route
``(provider, normalized base_url, credential_identity)`` at the Hermes ROOT (shared by
every profile), so sibling instances sharing the same billed account skip the benched
primary before their first call, while separate accounts on the same endpoint remain
isolated. It mirrors ``agent.nous_rate_guard``'s shared-file pattern but generalizes
it to any provider's account-level billing refusal.

Contract: FAIL OPEN. Any read/write/parse error returns "not benched" / no-op, so a
broken sidecar can never block a request that might otherwise succeed. Only a genuine
billing failover arms it; a successful call on the route clears it. Raw secrets are
never stored on disk — only a decoded non-secret account principal or a short hash.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

from utils import atomic_write_text

logger = logging.getLogger(__name__)

# Account-level credit exhaustion does not reset in seconds; default to a 30-min bench
# unless the provider declared a reset. Tunable via fallback.billing_cooldown_seconds.
_DEFAULT_BILLING_COOLDOWN_S = 1800.0
# A provider-declared reset further out than this is clamped: a stale/absurd reset must
# not bench the primary for days on one 402.
_MAX_BILLING_COOLDOWN_S = 4 * 3600.0


def _state_path() -> str:
    """Shared billing-cooldown file at the Hermes ROOT (cross-profile)."""
    try:
        from hermes_constants import get_default_hermes_root
        base = get_default_hermes_root()
    except Exception:
        from hermes_constants import get_hermes_home
        base = get_hermes_home()
    return os.path.join(str(base), "rate_limits", "billing.json")


def agent_credential_hint(agent: Any) -> str:
    """Extract the live credential string from *agent* without mutating state."""
    return str(
        getattr(agent, "api_key", None)
        or getattr(agent, "_anthropic_api_key", None)
        or ""
    ).strip()


def _credential_identity(provider: str, api_key: str = "", credential_id: str = "") -> str:
    """Non-secret account/credential discriminator; raw secrets never leave memory.

    Prefers a decoded stable principal (Codex JWT ``chatgpt_account_id`` + ``sub``)
    so OAuth token refreshes preserve the slot; otherwise falls back to a short
    SHA-256 fingerprint of the credential, or an explicit *credential_id*.
    """
    token = str(api_key or "").strip()
    p = str(provider or "").strip().lower()
    if token and p == "openai-codex":
        try:
            from agent.credential_pool import _codex_principal_identity
            principal = _codex_principal_identity(token)
            if principal:
                return f"codex:{principal[0]}:{principal[1]}"
        except Exception:
            pass
    if token:
        return f"fp:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"
    cid = str(credential_id or "").strip()
    return f"cid:{cid}" if cid else ""


def _route_key(
    provider: str, base_url: str, *, api_key: str = "", credential_id: str = "",
) -> str:
    """Account-scoped route key: provider + normalized endpoint + credential identity.
    Model is deliberately NOT in the key — a billing refusal benches every model
    on that account, while leaving other accounts on the same endpoint untouched."""
    from hermes_cli.route_identity import normalize_route_base_url
    p = str(provider or "").strip().lower()
    u = normalize_route_base_url(base_url or "")
    cred = _credential_identity(p, api_key, credential_id)
    return f"{p}|{u}|{cred}"


def _load() -> dict:
    try:
        with open(_state_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def _default_cooldown() -> float:
    try:
        from hermes_cli.config import load_config
        val = float((load_config() or {}).get("fallback", {}).get("billing_cooldown_seconds")
                    or _DEFAULT_BILLING_COOLDOWN_S)
        return val if val > 0 else _DEFAULT_BILLING_COOLDOWN_S
    except Exception:
        return _DEFAULT_BILLING_COOLDOWN_S


def record_billing_cooldown(
    provider: str, base_url: str, *, api_key: str = "", credential_id: str = "", reset_at=None,
) -> None:
    """Arm the shared billing cooldown for an account route after a genuine billing failover."""
    key = _route_key(provider, base_url, api_key=api_key, credential_id=credential_id)
    if not key.strip("|"):
        return
    now = time.time()
    until = None
    if reset_at is not None:
        try:
            from agent.credential_pool import _parse_absolute_timestamp
            parsed = _parse_absolute_timestamp(reset_at)
            if parsed is not None and parsed > now:
                until = min(parsed, now + _MAX_BILLING_COOLDOWN_S)
        except Exception:
            until = None
    if until is None:
        until = now + min(_default_cooldown(), _MAX_BILLING_COOLDOWN_S)
    data = _load()
    # Never EXTEND an active bench: the pre-call guard re-activates fallback on each
    # skipped call, and re-recording must not push the reset later forever. Keep the
    # earliest known reset so the bench always expires and the primary gets retried.
    prev = data.get(key)
    if isinstance(prev, dict):
        try:
            prev_reset = float(prev.get("reset_at", 0))
            if prev_reset > now:
                until = min(until, prev_reset)
        except (TypeError, ValueError):
            pass
    data[key] = {"reset_at": until, "recorded_at": now}
    try:
        atomic_write_text(_state_path(), json.dumps(data))
        logger.info("Billing cooldown recorded for %s: %.0fs", key, until - now)
    except Exception as exc:
        logger.debug("Failed to write billing cooldown state: %s", exc)


def billing_cooldown_remaining(
    provider: str, base_url: str, *, api_key: str = "", credential_id: str = "",
) -> Optional[float]:
    """Seconds left on the account route's billing cooldown, or None when not benched."""
    key = _route_key(provider, base_url, api_key=api_key, credential_id=credential_id)
    entry = _load().get(key)
    if not isinstance(entry, dict):
        return None
    try:
        remaining = float(entry.get("reset_at", 0)) - time.time()
    except (TypeError, ValueError):
        return None
    if remaining > 0:
        return remaining
    # Expired: opportunistically drop just this key (best-effort, never raises).
    data = _load()
    if key in data:
        data.pop(key, None)
        with contextlib.suppress(Exception):
            atomic_write_text(_state_path(), json.dumps(data))
    return None


def clear_billing_cooldown(
    provider: str, base_url: str, *, api_key: str = "", credential_id: str = "",
) -> None:
    """Clear the account route's billing cooldown (e.g. after a successful call on it)."""
    key = _route_key(provider, base_url, api_key=api_key, credential_id=credential_id)
    data = _load()
    if key not in data:
        return
    data.pop(key, None)
    with contextlib.suppress(Exception):
        atomic_write_text(_state_path(), json.dumps(data))
