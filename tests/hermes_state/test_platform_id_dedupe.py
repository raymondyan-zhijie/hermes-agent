"""DB-level platform-id dedupe (#104653): the same accepted turn must not be persisted twice.

The gateway writes the inbound user row on receipt (append_message) and the agent's turn-end flush
re-inserts it (append_messages_batch). Each writer's _DB_PERSISTED_MARKER lives in memory, so neither
can see the other's committed row; the guard therefore checks the DB inside the write transaction.

The identity is (session, platform id, gateway_input_owner) — never content or the platform id alone:
two independently accepted identical inputs must both survive (see test_codex_echo_ownership.py and
evals/gateway_failure_ownership).
"""
import sqlite3

from hermes_state import SessionDB


def _db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("sess", source="cli")
    return db


def _rows(db_path, session_id, mid):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND platform_message_id = ?",
            (session_id, mid)).fetchone()[0]
    finally:
        con.close()


def test_same_turn_written_by_both_writers_is_stored_once(tmp_path):
    """Path A (gateway) then path B (agent flush), same owner marker -> one row."""
    db = _db(tmp_path)
    md = {"gateway_input_owner": "owner-1"}
    db.append_message(session_id="sess", role="user", content="查", platform_message_id="om-1",
                      display_metadata=dict(md))
    db.append_messages_batch("sess", [{"role": "user", "content": "查", "platform_message_id": "om-1",
                                       "display_metadata": dict(md)}])
    assert _rows(db.db_path, "sess", "om-1") == 1
    db.close()


def test_batch_then_single_also_deduped(tmp_path):
    """The reverse order (agent flush first, gateway append second) dedupes too."""
    db = _db(tmp_path)
    md = {"gateway_input_owner": "owner-2"}
    db.append_messages_batch("sess", [{"role": "user", "content": "hi", "platform_message_id": "om-2",
                                       "display_metadata": dict(md)}])
    db.append_message(session_id="sess", role="user", content="hi", platform_message_id="om-2",
                      display_metadata=dict(md))
    assert _rows(db.db_path, "sess", "om-2") == 1
    db.close()


def test_independently_accepted_inputs_with_same_id_both_survive(tmp_path):
    """Distinct owner markers = two independent accepted turns, even with one platform id."""
    db = _db(tmp_path)
    db.append_message(session_id="sess", role="user", content="A", platform_message_id="om-3",
                      display_metadata={"gateway_input_owner": "owner-A"})
    db.append_message(session_id="sess", role="user", content="B", platform_message_id="om-3",
                      display_metadata={"gateway_input_owner": "owner-B"})
    assert _rows(db.db_path, "sess", "om-3") == 2
    db.close()


def test_unowned_rows_are_never_deduped(tmp_path):
    """No owner marker -> no identity to match, so both writes stay (codex echo invariant)."""
    db = _db(tmp_path)
    db.append_message(session_id="sess", role="user", content="same", platform_message_id="om-4")
    db.append_message(session_id="sess", role="user", content="same", platform_message_id="om-4")
    assert _rows(db.db_path, "sess", "om-4") == 2
    db.close()


def test_observed_echo_never_owns_and_never_absorbed(tmp_path):
    """An observed echo row (observed=1) does not own the input: the owning write still lands."""
    db = _db(tmp_path)
    md = {"gateway_input_owner": "owner-5"}
    db.append_message(session_id="sess", role="user", content="q", platform_message_id="om-5",
                      observed=True, display_metadata=dict(md))
    db.append_message(session_id="sess", role="user", content="q", platform_message_id="om-5",
                      display_metadata=dict(md))
    assert _rows(db.db_path, "sess", "om-5") == 2
    db.close()


def test_other_session_and_soft_archived_row_do_not_block(tmp_path):
    """Identity is per session, and a soft-archived (rewind/replace) row never blocks a re-insert."""
    db = _db(tmp_path)
    db.create_session("other", source="cli")
    md = {"gateway_input_owner": "owner-6"}
    db.append_message(session_id="sess", role="user", content="x", platform_message_id="om-6",
                      display_metadata=dict(md))
    db.append_message(session_id="other", role="user", content="x", platform_message_id="om-6",
                      display_metadata=dict(md))
    assert _rows(db.db_path, "other", "om-6") == 1

    db.append_message(session_id="sess", role="user", content="p", platform_message_id="om-7",
                      display_metadata={"gateway_input_owner": "owner-7"})
    db.replace_messages("sess", [{"role": "user", "content": "p2", "platform_message_id": "om-7",
                                  "display_metadata": {"gateway_input_owner": "owner-7"}}],
                        archive_dropped=True)
    assert _rows(db.db_path, "sess", "om-7") == 2  # archived + re-inserted live row
    db.close()
