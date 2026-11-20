from __future__ import annotations

import time

import pytest

from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import NewSession
from persistence.core.identity import SessionId

SESSION = SessionId("dedupe-s1")


@pytest.fixture(autouse=True)
def _sqlite_only(backend_name):
    if backend_name != "sqlite":
        pytest.skip("platform-id dedupe is implemented in the production SQLite backend")


def msg(text: str, pid: str | None, owner: str | None = None) -> NewMessage:
    md = {"gateway_input_owner": owner} if owner else None
    return NewMessage(role="user", content=text, timestamp=time.time(),
                      platform_message_id=pid, display_metadata=md)


def seed(backend, scope):
    store = backend.open(scope)
    store.sessions.create(NewSession(session_id=SESSION, source="cli"))
    return store


def test_single_append_dedupes_same_platform_and_owner(backend, scope_a):
    store = seed(backend, scope_a)
    repo = store.messages
    first = repo.append(SESSION, msg("hello", "tg-1", "owner-A"))
    second = repo.append(SESSION, msg("hello", "tg-1", "owner-A"))
    assert int(first) == int(second)
    assert len(repo.read_conversation(SESSION)) == 1


def test_batch_append_dedupes_same_platform_and_owner(backend, scope_a):
    store = seed(backend, scope_a)
    repo = store.messages
    first = repo.append(SESSION, msg("hello", "tg-2", "owner-A"))
    result = repo.append_batch(SESSION, [msg("hello", "tg-2", "owner-A")])
    assert len(result.outcomes) == 1
    assert int(result.outcomes[0].id) == int(first)
    assert len(repo.read_conversation(SESSION)) == 1


def test_different_owner_keeps_both(backend, scope_a):
    store = seed(backend, scope_a)
    repo = store.messages
    repo.append(SESSION, msg("hello", "tg-3", "owner-A"))
    repo.append(SESSION, msg("hello", "tg-3", "owner-B"))
    assert len(repo.read_conversation(SESSION)) == 2


def test_different_platform_id_keeps_both(backend, scope_a):
    store = seed(backend, scope_a)
    repo = store.messages
    repo.append(SESSION, msg("hello", "tg-4", "owner-A"))
    repo.append(SESSION, msg("hello", "tg-5", "owner-A"))
    assert len(repo.read_conversation(SESSION)) == 2


def test_no_owner_marker_never_dedupes(backend, scope_a):
    store = seed(backend, scope_a)
    repo = store.messages
    repo.append(SESSION, msg("hello", "tg-6", None))
    repo.append(SESSION, msg("hello", "tg-6", None))
    assert len(repo.read_conversation(SESSION)) == 2
