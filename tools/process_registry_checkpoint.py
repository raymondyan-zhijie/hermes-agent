"""Running-process checkpoint persistence and PID-safe recovery."""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.process_registry")
from pathlib import Path



class ProcessCheckpointMixin:
    # ----- Checkpoint (crash recovery) -----

    def _write_checkpoint(self, extra_entries: Optional[List[Dict[str, Any]]] = None):
        """Write running process metadata to the checkpoint file atomically."""
        from tools.process_registry import _checkpoint_path, _CHECKPOINT_FIELDS
        from utils import atomic_json_write

        try:
            with self._lock:
                entries = []
                for s in self._running.values():
                    if s.exited:
                        continue
                    # Backfill the start time so recovery can detect PID recycling
                    # even for sessions spawned before this field existed.
                    if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                        s.host_start_time = self._safe_host_start_time(s.pid)
                    entry = {"session_id": s.id, **{f: getattr(s, f) for f in _CHECKPOINT_FIELDS}}
                    # Redact inline credentials before persisting (~/.hermes/processes.json).
                    # Recovery uses command only for display (adoption re-validates the
                    # PID, never re-runs it), so masking is lossless.
                    # See #77484.
                    entry["command"] = redact_sensitive_text(s.command, code_file=True)
                    entry["owner_task_id"] = s.owner_task_id or s.task_id
                    entries.append(entry)
                if extra_entries:
                    tracked_ids = {item.get("session_id") for item in entries}
                    entries.extend(item for item in extra_entries if item.get("session_id") not in tracked_ids)
                # Group by OWNING profile -- never by the ambient HERMES_HOME at write time:
                # each profile's file holds only its own entries (P1-e).
                # Resolve to PATHS before writing: two entries can resolve to the same file
                # (a stale profile name falls back to this scope's own checkpoint), and writing
                # that file twice would let the second group clobber the first.
                # Snapshot -> resolve -> write -> reset all happen under ONE lock on purpose: with
                # the writes (and the [] resets) outside it, a second thread's snapshot taken
                # before a process exited could reset the just-written file to [] and silently
                # drop a live entry.
                by_path: Dict[Any, List[Dict[str, Any]]] = {}
                for entry in entries:
                    path = checkpoint_path_for_profile(profile_from_entry(entry))
                    by_path.setdefault(path, []).append(entry)
                # This scope's own file is ALWAYS written, even with zero entries: it must mirror
                # the live registry, so "everything exited" has to land as [] rather than leaving
                # the previous snapshot's ghost rows in place forever (the pre-grouping code
                # rewrote the file unconditionally, which is what kept that invariant).
                by_path.setdefault(_checkpoint_path(), [])
                # setdefault keeps the mixin usable standalone (unit tests build it directly)
                written_paths = self.__dict__.setdefault("_ckpt_files_written", set())
                written: set = set()
                for path, items in by_path.items():
                    try:
                        atomic_json_write(path, items)
                    except Exception:
                        # One unwritable path (a stale profile home, a read-only mount) must not
                        # abort the rest: the profiles later in this batch would lose their
                        # checkpoint too and come back orphaned after a restart (that is exactly
                        # what a single bogus ``profiles/<name>`` path used to do).
                        logger.warning(
                            "Failed to write checkpoint to %s (%d entries)", path, len(items),
                            exc_info=True)
                        continue
                    written.add(path)
                    written_paths.add(path)
                # A profile whose last process exited must go back to [] -- otherwise its
                # file keeps ghost rows forever.  Only files this instance wrote are
                # reset, so unrelated profiles are never touched.
                for path in list(written_paths):
                    if path not in written:
                        try:
                            atomic_json_write(path, [])
                        except Exception:
                            logger.warning("Failed to reset checkpoint file %s", path,
                                           exc_info=True)
                            continue
                        written_paths.discard(path)
        except Exception as e:
            logger.debug("Failed to write checkpoint file: %s", e, exc_info=True)

    def recover_from_checkpoint(self) -> int:
        """On gateway startup, probe PIDs from the checkpoint file; returns how many
        were recovered as detached sessions."""
        from tools.process_registry import (
            ProcessSession, _CHECKPOINT_FIELDS, _checkpoint_path,
            _CHECKPOINT_DEFAULTS, _WATCHER_ROUTE_KEYS, _stop_systemd_unit,
        )

        checkpoint_path = _checkpoint_path()
        if not checkpoint_path.exists():
            return 0
        try:
            entries = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        recovered = 0
        unresolved_scope_entries: List[Dict[str, Any]] = []
        for entry in entries:
            pid, pid_scope = entry.get("pid"), entry.get("pid_scope", "host")
            if not pid:
                continue
            # The registry is process-global, so every profile's checkpoint carries every live
            # process; a multiplexer recovering several homes must adopt each session once.
            with self._lock:
                already_tracked = entry.get("session_id") in self._running
            if already_tracked:
                continue
            if pid_scope != "host":  # in-sandbox PIDs mean nothing once the env handle is gone
                logger.info(
                    "Skipping recovery for non-host process: %s (pid=%s, scope=%s)",
                    entry.get("command", "unknown")[:60], pid, pid_scope)
                continue
            # Alive AND the same process: across a restart the kernel may have
            # recycled the PID onto a stranger, and adopting it would let a later
            # kill tree-kill e.g. a browser.
            if not self._host_pid_is_ours(pid, entry.get("host_start_time")):
                if self._is_host_pid_alive(pid):
                    logger.info(
                        "Not recovering session %s: pid %d is alive but its "
                        "start time no longer matches — PID was recycled onto "
                        "an unrelated process; refusing to adopt it.",
                        entry.get("session_id", "?"), pid)
                systemd_unit = entry.get("systemd_unit", "")
                if systemd_unit and not _stop_systemd_unit(systemd_unit):
                    logger.warning(
                        "Could not reap persisted scope %s for dead wrapper pid %s; "
                        "retaining checkpoint entry for the next startup",
                        systemd_unit, pid)
                    unresolved_scope_entries.append(entry)
                continue
            fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
            fields.update(
                command=entry.get("command", "unknown"),
                owner_task_id=entry.get("owner_task_id", "") or entry.get("task_id", ""),
                started_at=entry.get("started_at", time.time()))
            # detached: can't read output, but can report status + kill
            session = ProcessSession(id=entry["session_id"], detached=True, **fields)
            with self._lock:
                self._running[session.id] = session
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session.command[:60], pid)
            # Re-enqueue watcher so gateway can resume notifications
            if session.watcher_interval > 0:
                self.pending_watchers.append({
                    "session_id": session.id,
                    "check_interval": session.watcher_interval,
                    "session_key": session.session_key,
                    **{key: getattr(session, f"watcher_{key}") for key in _WATCHER_ROUTE_KEYS},
                    "notify_on_complete": session.notify_on_complete,
                    "parent_session_id": session.parent_session_id,
                })
        self._write_checkpoint(extra_entries=unresolved_scope_entries)
        return recovered



def _canonical_session_profile(namespace: str) -> str:
    """``<ns>`` slot of a session key -> owning profile id.

    Mirror of ``gateway/session.py:profile_from_session_key_namespace`` (the inverse of
    ``_session_key_namespace``): the DEFAULT profile's namespace is literally ``main``, while a
    profile literally named ``main`` is marked ``main~`` (``~`` sits outside the profile-id
    alphabet).  Reimplemented here rather than imported: ``tools`` must not depend on ``gateway``.
    """
    if namespace == "main":
        return "default"
    return "main" if namespace == "main~" else namespace


def profile_from_entry(entry: Dict[str, Any]) -> str:
    """Owning profile of a checkpoint entry: explicit field -> task_id/session_key -> "" (ambient).

    The multiplexed gateway serves every profile from ONE process, so the ambient
    ``HERMES_HOME`` at write time is NOT the owning profile of a tracked process
    (P1-e: a default-profile background process was persisted into
    ``profiles/design/processes.json``).  The spawn-time session profile is the
    truth; the task id is the fallback for entries written before the field existed.
    """
    import re  # local: this module has no top-level ``re`` import

    v = str(entry.get("profile") or "").strip()
    if v:
        return v
    # ``~`` is outside the profile-id alphabet but IS the marker of a profile literally named
    # ``main`` -- leaving it out of the class made ``agent:main~:`` fall through to "" (ambient).
    pat = re.compile(r"^(?:session:)?agent:([A-Za-z0-9_.~-]+):")
    for key in ("owner_task_id", "task_id", "session_key"):
        m = pat.match(str(entry.get(key) or "").strip())
        if m:
            # NEVER return the raw namespace: ``agent:main:`` is the DEFAULT profile, and
            # inventing a "main" directory would both lose the entry and derail the write
            # (see ``checkpoint_path_for_profile``).
            return _canonical_session_profile(m.group(1))
    return ""


def checkpoint_path_for_profile(profile: str) -> Path:
    """``processes.json`` owned by *profile*.

    ``""`` -> this scope's authoritative ``_checkpoint_path()`` (the pre-existing behaviour: an
    entry with no owner information belongs to whoever is writing it, and tests/CLI runs may
    redirect ``CHECKPOINT_PATH``).  ``"default"`` -> the ROOT home's file, because the default
    profile IS the launch home even when this process is serving another profile.  A named
    profile -> its own home's file, but ONLY when that home exists:
    ``atomic_json_write`` -> ``mkdir_under_hermes_home`` REFUSES a missing named-profile home
    with ``FileNotFoundError`` (it asserts before mkdir), and that exception aborts the whole
    checkpoint write -- every profile later in the batch silently loses its file.  A stale name
    therefore degrades to this scope's checkpoint instead of aborting.
    """
    from tools.process_registry import _checkpoint_path  # lazy: 环形导入规避
    from hermes_constants import get_default_hermes_root
    if not profile:
        return _checkpoint_path()
    root = get_default_hermes_root()
    if profile == "default":
        return root / "processes.json"
    target = root / "profiles" / profile / "processes.json"
    if not target.parent.is_dir():
        logger.warning(
            "Checkpoint entry claims profile %r whose home does not exist; keeping it in this "
            "scope's own checkpoint instead of creating %s", profile, target.parent)
        return _checkpoint_path()
    return target

