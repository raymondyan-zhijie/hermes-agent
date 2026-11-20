"""DB-level platform-id dedupe (#104653 / upstream PR #79610).

A gateway-accepted turn is identified by (session, platform id, gateway_input_owner) -- the
same predicate ``has_gateway_input_owner`` uses: a live OWNING row (``observed = 0``) carrying
the same marker. Only then may a second write of the SAME accepted turn be skipped.

Deliberately NOT keyed on content or on the platform id alone: two independently accepted
identical inputs (same text, even the same platform id) must both survive -- tests/agent/
test_codex_echo_ownership.py and evals/gateway_failure_ownership pin this. Rows without an
owner marker are never deduped here. Scoped to ACTIVE USER rows on purpose: rewind/replace/
compaction soft-archive the superseded row in the same txn before re-inserting, so a re-insert
stays legal.

Runs INSIDE the caller's write transaction (the same guarantee the repair path relies on: the
active set cannot change between this read and the INSERT).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Optional


_ACTIVE_PLATFORM_ROW_SQL = (
    "SELECT * FROM messages WHERE session_id = ? AND platform_message_id = ? AND active = 1 "
    "AND observed = 0 AND role = 'user' AND CASE WHEN json_valid(display_metadata) THEN"
    " json_extract(display_metadata, '$.gateway_input_owner') END = ?"
    " ORDER BY id LIMIT 1"
)


def gateway_input_owner_of(msg: Mapping[str, Any]) -> Optional[str]:
    """``gateway_input_owner`` marker of a row dict, or None.

    ``display_metadata`` is a live dict on the agent flush path and an already-encoded JSON
    string on the gateway append path.
    """
    md = msg.get("display_metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except (TypeError, ValueError):
            return None
    owner = md.get("gateway_input_owner") if isinstance(md, dict) else None
    return owner if isinstance(owner, str) and owner else None


def find_owned_platform_row(
    conn: sqlite3.Connection, session_id: str, platform_message_id: Any,
    owner: Optional[str],
) -> Optional[sqlite3.Row]:
    """The live owning row for this accepted input, or None. Caller holds the write txn."""
    if not owner or platform_message_id in (None, ""):
        return None
    return conn.execute(
        _ACTIVE_PLATFORM_ROW_SQL, (session_id, str(platform_message_id), owner)
    ).fetchone()
