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
"""
from __future__ import annotations

import json
import threading

import pytest

import hermes_constants
import tools.process_registry as pr
from tools.process_registry import checkpoint_path_for_profile, profile_from_entry
from tools.process_registry_checkpoint import ProcessCheckpointMixin


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """Environment root with two LIVE named profiles (and deliberately no profiles/main)."""
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    for name in ("ops", "research"):
        (tmp_path / "profiles" / name).mkdir(parents=True, exist_ok=True)
    return tmp_path


# ── entry -> owning profile ───────────────────────────────────────────────────────────────
def test_legacy_default_namespace_is_canonicalized_to_default(root):
    entry = {"task_id": "session:agent:main:feishu:dm:oc_x",
             "session_key": "agent:main:feishu:dm:oc_x"}
    assert profile_from_entry(entry) == "default"
    assert checkpoint_path_for_profile("default") == root / "processes.json"


def test_legacy_marked_main_profile_maps_to_main(root):
    # ``~`` marks a profile literally named ``main``; it is outside the profile-id alphabet.
    assert profile_from_entry({"session_key": "agent:main~:feishu:dm:oc_x"}) == "main"


def test_named_profile_namespace(root):
    assert profile_from_entry({"session_key": "agent:research:feishu:dm:oc_x"}) == "research"
    assert (checkpoint_path_for_profile("research")
            == root / "profiles" / "research" / "processes.json")


def test_explicit_profile_field_wins_over_the_session_key(root):
    assert profile_from_entry(
        {"profile": "ops", "session_key": "agent:research:feishu:dm:oc_x"}) == "ops"


def test_owner_task_id_is_consulted_before_task_id(root):
    assert profile_from_entry(
        {"owner_task_id": "session:agent:ops:x", "task_id": "session:agent:research:x"}) == "ops"


def test_ambient_entry_uses_this_scopes_checkpoint(root, monkeypatch):
    """No owner information -> whoever is writing owns it (CHECKPOINT_PATH stays respected)."""
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: root / "processes.json")
    assert profile_from_entry({"session_key": ""}) == ""
    assert checkpoint_path_for_profile("") == root / "processes.json"


def test_unknown_profile_falls_back_and_never_materializes_a_home(root, monkeypatch):
    """A stale profile name must stay writable WITHOUT creating profiles/<name>."""
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: root / "processes.json")
    assert not (root / "profiles" / "ghost").exists()

    assert checkpoint_path_for_profile("ghost") == root / "processes.json"
    assert not (root / "profiles" / "ghost").exists()


def test_canonicalisation_matches_gateway_session_semantics(root):
    """Parity guard: gateway/session.py owns this mapping; drift here re-opens the bug."""
    from gateway.session import _session_key_namespace, profile_from_session_key_namespace
    from tools.process_registry import _canonical_session_profile

    for profile in (None, "default", "main", "ops", "research"):
        namespace = _session_key_namespace(profile).split(":", 2)[1]
        assert _canonical_session_profile(namespace) == profile_from_session_key_namespace(namespace)


# ── _write_checkpoint: routing, isolation, reset ──────────────────────────────────────────
class _Session:
    """Minimal ProcessSession stand-in (only _CHECKPOINT_FIELDS are read)."""

    exited = False
    pid = 4242
    pid_scope = "host"
    command = "true"
    cwd = "/"
    systemd_unit = ""
    host_start_time = 1
    started_at = 0.0
    parent_session_id = ""
    watcher_platform = watcher_chat_id = watcher_user_id = ""
    watcher_user_name = watcher_thread_id = watcher_message_id = ""
    watcher_interval = 0
    notify_on_complete = False
    completion_output_chars = 0
    watch_patterns = ()

    def __init__(self, session_id, task_id, session_key, profile=""):
        self.id = session_id
        self.task_id = self.owner_task_id = task_id
        self.session_key = session_key
        self.profile = profile


class _Registry(ProcessCheckpointMixin):
    """The mixin documents standalone construction for unit tests; this is that construction."""

    _safe_host_start_time = staticmethod(lambda pid: 1)

    def __init__(self, sessions):
        self._running = {s.id: s for s in sessions}
        self._lock = threading.Lock()


def _setup(root, monkeypatch):
    ambient = root / "processes.json"
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: ambient)
    return ambient


def test_write_routes_each_entry_to_its_owner_and_creates_nothing_extra(root, monkeypatch):
    ambient = _setup(root, monkeypatch)
    registry = _Registry([
        # Legacy entry whose session key names the DEFAULT profile (the real misfiled case).
        _Session("p_main", "session:agent:main:feishu:dm:x", "agent:main:feishu:dm:x"),
        _Session("p_ops", "session:agent:ops:feishu:dm:x", "agent:ops:feishu:dm:x", "ops"),
    ])

    registry._write_checkpoint()

    assert [e["session_id"] for e in json.loads(ambient.read_text())] == ["p_main"]
    ops = json.loads((root / "profiles" / "ops" / "processes.json").read_text())
    assert [e["session_id"] for e in ops] == ["p_ops"]
    assert not (root / "profiles" / "main").exists()


def test_stale_profile_name_does_not_abort_the_other_profiles(root, monkeypatch):
    """Regression: one bogus path used to swallow the WHOLE batch (orphaned processes)."""
    ambient = _setup(root, monkeypatch)
    registry = _Registry([
        _Session("p_ghost", "session:agent:ghost:feishu:dm:x", "agent:ghost:feishu:dm:x"),
        _Session("p_ops", "session:agent:ops:feishu:dm:x", "agent:ops:feishu:dm:x", "ops"),
    ])

    registry._write_checkpoint()

    assert not (root / "profiles" / "ghost").exists()
    # The stale entry degrades to this scope's own checkpoint instead of vanishing.
    assert [e["session_id"] for e in json.loads(ambient.read_text())] == ["p_ghost"]
    assert [e["session_id"] for e in json.loads(
        (root / "profiles" / "ops" / "processes.json").read_text())] == ["p_ops"]


def test_two_entries_resolving_to_the_same_file_do_not_clobber_each_other(root, monkeypatch):
    ambient = _setup(root, monkeypatch)
    registry = _Registry([
        _Session("p_ghost", "session:agent:ghost:feishu:dm:x", "agent:ghost:feishu:dm:x"),
        _Session("p_main", "session:agent:main:feishu:dm:x", "agent:main:feishu:dm:x"),
    ])

    registry._write_checkpoint()

    assert {e["session_id"] for e in json.loads(ambient.read_text())} == {"p_ghost", "p_main"}


def test_profile_file_is_reset_once_its_last_process_is_gone(root, monkeypatch):
    ambient = _setup(root, monkeypatch)
    main = _Session("p_main", "session:agent:main:feishu:dm:x", "agent:main:feishu:dm:x")
    ops = _Session("p_ops", "session:agent:ops:feishu:dm:x", "agent:ops:feishu:dm:x", "ops")
    registry = _Registry([main, ops])
    ops_file = root / "profiles" / "ops" / "processes.json"

    registry._write_checkpoint()
    assert json.loads(ops_file.read_text())

    registry._running = {main.id: main}
    registry._write_checkpoint()
    assert json.loads(ops_file.read_text()) == []
