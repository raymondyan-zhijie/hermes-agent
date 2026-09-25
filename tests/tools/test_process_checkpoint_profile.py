"""Owning-profile routing for the process checkpoint (P1-e).

A checkpoint entry must land in the file of the profile that OWNS it -- derived from the
spawn-time session profile or, for legacy entries, from the session key.  The default
profile's session namespace is literally ``main``
(``gateway/session.py:_session_key_namespace``), so the legacy fallback MUST canonicalize
``main -> default``: a raw ``main`` resolves to ``profiles/main/processes.json``, which no
reader ever visits AND which cannot even be written -- ``atomic_json_write`` ->
``mkdir_under_hermes_home`` refuses a non-existent named-profile home with
``FileNotFoundError``, and that exception used to abort the whole checkpoint write, silently
costing every OTHER profile in the same batch its file (orphaned processes after a restart).

**零依赖可执行（2026-09-25）**：本文件此前依赖 ``pytest``（``import pytest`` +
``@pytest.fixture``），而生产 venv 既无 pytest 也无 pip、系统 python 同样没有 ⇒
登记册里那条验收命令在本机**根本跑不了**。现改为：0 参测试 + 内置 ``_env()`` 上下文
（临时根 + 自带 monkeypatch）+ 文件末尾自跑 ``__main__`` ⇒
``python3 <本文件>`` 或 ``run-in-tree.py <TREE> <本文件>`` 都能跑，pytest 亦可直接收集。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import hermes_constants
import tools.process_registry as pr
from tools.process_registry_checkpoint import (ProcessCheckpointMixin,
                                               checkpoint_path_for_profile,
                                               profile_from_entry)


class _Patch:
    """Minimal monkeypatch: records the original values and restores them (LIFO)."""

    def __init__(self) -> None:
        self._saved = []

    def setattr(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self) -> None:
        while self._saved:
            obj, name, value = self._saved.pop()
            setattr(obj, name, value)


@contextmanager
def _env(checkpoint: bool = True, profiles=("ops", "research")):
    """Temp environment root with LIVE named profiles (deliberately no ``profiles/main``)."""
    root = Path(tempfile.mkdtemp(prefix="ckpt-profile-"))
    for name in profiles:
        (root / "profiles" / name).mkdir(parents=True, exist_ok=True)
    patch = _Patch()
    patch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    ambient = None
    if checkpoint:
        ambient = root / "processes.json"
        patch.setattr(pr, "_checkpoint_path", lambda: ambient)
    try:
        yield root, ambient
    finally:
        patch.undo()
        shutil.rmtree(root, ignore_errors=True)


# ── entry -> owning profile ───────────────────────────────────────────────────────────────
def test_legacy_default_namespace_is_canonicalized_to_default():
    with _env() as (root, _):
        entry = {"task_id": "session:agent:main:feishu:dm:oc_x",
                 "session_key": "agent:main:feishu:dm:oc_x"}
        assert profile_from_entry(entry) == "default"
        assert checkpoint_path_for_profile("default") == root / "processes.json"


def test_legacy_marked_main_profile_maps_to_main():
    with _env() as (_, __):
        # ``~`` marks a profile literally named ``main``; it is outside the profile-id alphabet.
        assert profile_from_entry({"session_key": "agent:main~:feishu:dm:oc_x"}) == "main"


def test_named_profile_namespace():
    with _env() as (root, _):
        assert profile_from_entry({"session_key": "agent:research:feishu:dm:oc_x"}) == "research"
        assert (checkpoint_path_for_profile("research")
                == root / "profiles" / "research" / "processes.json")


def test_explicit_profile_field_wins_over_the_session_key():
    """Dict-level contract of ``profile_from_entry`` (the field is optional)."""
    with _env() as (_, __):
        assert profile_from_entry(
            {"profile": "ops", "session_key": "agent:research:feishu:dm:oc_x"}) == "ops"


def test_owner_task_id_is_consulted_before_task_id():
    with _env() as (_, __):
        assert profile_from_entry(
            {"owner_task_id": "session:agent:ops:x",
             "task_id": "session:agent:research:x"}) == "ops"


def test_ambient_entry_uses_this_scopes_checkpoint():
    """No owner information -> whoever is writing owns it (CHECKPOINT_PATH stays respected)."""
    with _env() as (root, ambient):
        assert profile_from_entry({"session_key": ""}) == ""
        assert checkpoint_path_for_profile("") == ambient


def test_unknown_profile_falls_back_and_never_materializes_a_home():
    """A stale profile name must stay writable WITHOUT creating profiles/<name>."""
    with _env() as (root, ambient):
        assert not (root / "profiles" / "ghost").exists()
        assert checkpoint_path_for_profile("ghost") == ambient
        assert not (root / "profiles" / "ghost").exists()


def test_canonicalisation_matches_gateway_session_semantics():
    """Parity guard: gateway/session.py owns this mapping; drift here re-opens the bug."""
    from gateway.session import _session_key_namespace, profile_from_session_key_namespace
    from tools.process_registry_checkpoint import _canonical_session_profile

    with _env() as (_, __):
        for profile in (None, "default", "main", "ops", "research"):
            namespace = _session_key_namespace(profile).split(":", 2)[1]
            assert _canonical_session_profile(namespace) == profile_from_session_key_namespace(namespace)


# ── _write_checkpoint: routing, isolation, reset ──────────────────────────────────────────
class _Session:
    """Minimal ProcessSession stand-in: every ``_CHECKPOINT_FIELDS`` key is filled from upstream's
    own ``_CHECKPOINT_DEFAULTS`` (itself derived from the dataclass), so an upstream field
    addition cannot silently break this test again.

    Regression note (2026-09-25): the previous hard-coded attribute list went stale when upstream
    added ``heartbeat_seconds`` — ``_write_checkpoint`` raised AttributeError into its
    ``except`` and the four write-path cases below failed. Nobody noticed because this file
    needs pytest, which this host does not have (no pytest, no pip in the venv).
    """

    def __init__(self, session_id, task_id, session_key):
        for field in pr._CHECKPOINT_FIELDS:
            setattr(self, field, pr._CHECKPOINT_DEFAULTS[field])
        self.exited = False
        self.pid = 4242
        self.id = session_id
        self.task_id = self.owner_task_id = task_id
        self.session_key = session_key


class _Registry(ProcessCheckpointMixin):
    """The mixin documents standalone construction for unit tests; this is that construction."""

    _safe_host_start_time = staticmethod(lambda pid: 1)

    def __init__(self, sessions):
        self._running = {s.id: s for s in sessions}
        self._lock = threading.Lock()


def test_write_routes_each_entry_to_its_owner_and_creates_nothing_extra():
    with _env() as (root, ambient):
        registry = _Registry([
            # Legacy entry whose session key names the DEFAULT profile (the real misfiled case).
            _Session("p_main", "session:agent:main:feishu:dm:x", "agent:main:feishu:dm:x"),
            _Session("p_ops", "session:agent:ops:feishu:dm:x", "agent:ops:feishu:dm:x"),
        ])
        registry._write_checkpoint()

        assert [e["session_id"] for e in json.loads(ambient.read_text())] == ["p_main"]
        ops = json.loads((root / "profiles" / "ops" / "processes.json").read_text())
        assert [e["session_id"] for e in ops] == ["p_ops"]
        assert not (root / "profiles" / "main").exists()


def test_stale_profile_name_does_not_abort_the_other_profiles():
    """Regression: one bogus path used to swallow the WHOLE batch (orphaned processes)."""
    with _env() as (root, ambient):
        registry = _Registry([
            _Session("p_ghost", "session:agent:ghost:feishu:dm:x", "agent:ghost:feishu:dm:x"),
            _Session("p_ops", "session:agent:ops:feishu:dm:x", "agent:ops:feishu:dm:x"),
        ])
        registry._write_checkpoint()

        assert not (root / "profiles" / "ghost").exists()
        # The stale entry degrades to this scope's own checkpoint instead of vanishing.
        assert [e["session_id"] for e in json.loads(ambient.read_text())] == ["p_ghost"]
        assert [e["session_id"] for e in json.loads(
            (root / "profiles" / "ops" / "processes.json").read_text())] == ["p_ops"]


def test_two_entries_resolving_to_the_same_file_do_not_clobber_each_other():
    with _env() as (root, ambient):
        registry = _Registry([
            _Session("p_ghost", "session:agent:ghost:feishu:dm:x", "agent:ghost:feishu:dm:x"),
            _Session("p_main", "session:agent:main:feishu:dm:x", "agent:main:feishu:dm:x"),
        ])
        registry._write_checkpoint()
        assert {e["session_id"] for e in json.loads(ambient.read_text())} == {"p_ghost", "p_main"}


def test_profile_file_is_reset_once_its_last_process_is_gone():
    with _env() as (root, _):
        ambient = root / "processes.json"
        main = _Session("p_main", "session:agent:main:feishu:dm:x", "agent:main:feishu:dm:x")
        ops = _Session("p_ops", "session:agent:ops:feishu:dm:x", "agent:ops:feishu:dm:x")
        registry = _Registry([main, ops])
        ops_file = root / "profiles" / "ops" / "processes.json"

        registry._write_checkpoint()
        assert json.loads(ops_file.read_text())

        registry._running = {main.id: main}
        registry._write_checkpoint()
        assert json.loads(ops_file.read_text()) == []
        assert [e["session_id"] for e in json.loads(ambient.read_text())] == ["p_main"]


# ── 自跑（无 pytest 也能用） ───────────────────────────────────────────────────────────────
def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    ok = fail = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {name}: {type(exc).__name__}: {exc}")
            fail += 1
    print(f"\n通过 {ok} · 失败 {fail}（共 {len(tests)}）")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_main())
