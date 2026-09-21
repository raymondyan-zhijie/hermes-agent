"""A1 dispatch pre-flight — hard block for cross-profile one-shot dispatches.

Why this exists
---------------
S1 (`work/skill-workshop/hermes-dispatch.sh`, 2026-09-21) made cross-bot dispatch
*visible*: the wrapper checks in-flight, registers the item and appends to
``runtime/dispatch-ledger.jsonl``, and the drift-check ``H`` item catches sessions
that skipped it — but only *after* the fact. A direct

    hermes -p <other-profile> chat -q "<prompt>"

still ran. This hook refuses it **before the agent starts**.

What is allowed (checked in order)
----------------------------------
1. ``target profile == caller profile`` — self runs, sub-agents, same-profile CLI.
2. ``caller unknown`` (no inherited ``HERMES_HOME``, i.e. a human/operator shell) —
   allowed and *audited*: the threat model is bot-to-bot dispatch, not the operator.
3. internal spawn markers: ``HERMES_CRON_SESSION`` (cron runs in-process, session ids
   ``cron_*``), ``HERMES_KANBAN_BOARD`` (kanban workers), A2A adapter.
4. a ``runtime/dispatch-ledger.jsonl`` row naming this target within ``TTL`` seconds —
   the sanctioned wrapper path.
5. explicit, audited escape: ``HERMES_DISPATCH_ALLOW=1`` **plus**
   ``HERMES_DISPATCH_ALLOW_REASON`` of >= 12 characters (troubleshooting only, always
   logged and pushed as a governance alert).

Otherwise: refuse with exit code 2, write an audit line and push an alert.

Boundary: defence in depth, not a security boundary — the same OS user can still
bypass it (e.g. by clearing the inherited environment). Its job is that a bypass can
no longer happen *by reflex*, and that every attempt is visible.
"""

from __future__ import annotations

import datetime as _dt
import json
import os

TTL_SECONDS = 20 * 60
REASON_MIN = 12

_WRAPPER = "work/skill-workshop/hermes-dispatch.sh"


def _base() -> str:
    return os.environ.get("GOVERNANCE_BASE", "/home/admin/.hermes")


def profile_of(home: str | None) -> str:
    """Profile name implied by a HERMES_HOME path ('' or None -> 'unknown').

    A bare name (no ``/``) is returned as-is, so callers may pass an already-resolved
    profile through the same path.
    """
    if not home:
        return "unknown"
    home = home.rstrip("/")
    if "/" not in home:
        return home
    parts = home.split("/")
    if len(parts) >= 2 and parts[-2] == "profiles":
        return parts[-1]
    return "default"


def _ledger_allows(target: str, now: _dt.datetime, ttl: int) -> dict | None:
    path = os.path.join(_base(), "runtime/dispatch-ledger.jsonl")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return None
    for row in reversed(rows):
        if str(row.get("to", "")) != target:
            continue
        try:
            ts = _dt.datetime.fromisoformat(row["ts"])
        except (KeyError, ValueError):
            continue
        if abs((now - ts).total_seconds()) <= ttl:
            return row
    return None


def _audit(action: str, name: str, detail: str) -> None:
    try:
        import sys

        sys.path.insert(0, os.path.join(_base(), "work/skill-workshop"))
        import audit_chain

        # The CLI can evaluate a launch twice (parent + re-exec), which would write the
        # same line twice. Skip a byte-identical detail written within 8 seconds.
        _log = os.path.join(_base(), "work/skill-workshop/audit-skill.md")
        try:
            with open(_log, encoding="utf-8") as fh:
                last = fh.readlines()[-1]
            if detail[:70] in last:
                stamp = last.split("|", 1)[0].strip()
                prev = _dt.datetime.fromisoformat(stamp)
                if abs((_dt.datetime.now().astimezone() - prev).total_seconds()) <= 8:
                    return
        except (OSError, IndexError, ValueError):
            pass
        audit_chain.append(action, name, "-", detail)
    except Exception:
        pass


def _alert(message: str) -> None:
    try:
        import subprocess

        script = os.path.join(_base(), "work/skill-workshop/governance_alert.py")
        if os.path.exists(script):
            subprocess.run(["python3", script, message], check=False, timeout=20)
    except Exception:
        pass


def evaluate(*, target_home: str | None, caller_home: str | None,
             source: str | None = None, argv: list | None = None,
             now: _dt.datetime | None = None, ttl: int = TTL_SECONDS) -> str | None:
    """Return ``None`` to allow the dispatch, or a block message to refuse it."""
    target = profile_of(target_home)
    caller = profile_of(caller_home)
    now = now or _dt.datetime.now().astimezone()
    if os.environ.get("HERMES_DISPATCH_DEBUG"):
        import sys as _s
        print(f"[a1-debug] pid={os.getpid()} target_home={target_home!r} caller_home={caller_home!r} "
              f"target={target} caller={caller} source={source!r} argv={' '.join(argv or [])[:110]!r}", file=_s.stderr)

    if caller == "unknown":
        _audit("dispatch-allow", target,
               f"A1 放行：发起方未知（人工 shell？） target={target} source={source} argv={' '.join(argv or [])[:120]}")
        return None
    if target == caller:
        return None
    if os.environ.get("HERMES_CRON_SESSION"):
        _audit("dispatch-allow", target, f"A1 放行：cron 内部 spawn target={target}")
        return None
    if os.environ.get("HERMES_KANBAN_BOARD"):
        _audit("dispatch-allow", target, f"A1 放行：kanban worker spawn target={target}")
        return None
    if str(source or "").lower() in {"a2a", "kanban"}:
        _audit("dispatch-allow", target, f"A1 放行：内部 source={source} target={target}")
        return None
    row = _ledger_allows(target, now, ttl)
    if row is not None:
        _audit("dispatch-allow", target,
               f"A1 放行：派单台账命中 carrier={row.get('carrier')} ts={row.get('ts')} ")
        return None
    if os.environ.get("HERMES_DISPATCH_ALLOW") == "1":
        reason = (os.environ.get("HERMES_DISPATCH_ALLOW_REASON") or "").strip()
        if len(reason) >= REASON_MIN:
            _audit("dispatch-allow", target,
                   f"A1 放行（显式逃逸，需业主授权）：target={target} 理由：{reason}")
            _alert(f"【治理·A1 逃逸使用】{now:%m-%d %H:%M} · 跨 profile 派单显式放行：{caller} → {target}；理由「{reason}」")
            return None
        return (f"⛔ 派单被拦（A1）：HERMES_DISPATCH_ALLOW=1 需同时给 "
                f"HERMES_DISPATCH_ALLOW_REASON（>= {REASON_MIN} 字，说明为何不走包装器）。")

    return (
        "⛔ 跨 profile 派单被硬拦（A1 · fork carry#16）\n"
        f"   发起：{caller}  →  目标：{target}   （source={source or 'cli'}）\n"
        "   未在 runtime/dispatch-ledger.jsonl 找到 20 分钟内的派单登记。\n"
        "   唯一合法入口（会做在途检查 + 登记 + 台账）：\n"
        f"     {_WRAPPER} --carrier <载体> --item \"<事项>\" --to {target} \\\n"
        f"          -- hermes -p {target} chat -q \"<prompt>\"\n"
        "   同 profile 自运行 / cron / kanban / 子代理不受影响；\n"
        "   排障确需直派：需业主授权，并置 HERMES_DISPATCH_ALLOW=1 + HERMES_DISPATCH_ALLOW_REASON。"
    )


def _caller_profile_from_env() -> str:
    """Caller profile as seen from the inherited environment.

    ``HERMES_DISPATCH_CALLER_HOME`` is captured by the CLI *before* the profile
    override rewrites HERMES_HOME; the session variables are the fallback for callers
    whose home was never exported (``agent:main:...`` == the default profile).
    """
    home = os.environ.get("HERMES_DISPATCH_CALLER_HOME")
    if home:
        return profile_of(home)
    prof = os.environ.get("HERMES_SESSION_PROFILE")
    if prof:
        return prof
    key = os.environ.get("HERMES_SESSION_KEY") or ""
    if key.startswith("agent:"):
        seg = key.split(":", 2)[1]
        return "default" if seg in {"main", "default"} else seg
    return "unknown"


def guard_dispatch(*, source: str | None = None, argv: list | None = None) -> None:
    """Raise ``SystemExit(2)`` when this one-shot dispatch must not proceed."""
    import sys

    message = evaluate(
        target_home=os.environ.get("HERMES_HOME"),
        caller_home=_caller_profile_from_env(),
        source=source,
        argv=argv if argv is not None else sys.argv,
    )
    if message is None:
        return
    _audit("dispatch-block", profile_of(os.environ.get("HERMES_HOME")),
           f"A1 硬拦跨 profile 派单：caller={_caller_profile_from_env()} "
           f"argv={' '.join(argv if argv is not None else sys.argv)[:160]}")
    _alert(f"【治理·A1 硬拦】{_dt.datetime.now().astimezone():%m-%d %H:%M} · 跨 profile 派单未登记："
           f"{_caller_profile_from_env()} → "
           f"{profile_of(os.environ.get('HERMES_HOME'))}（已拒绝，rc=2）")
    print(message, file=sys.stderr)
    raise SystemExit(2)
