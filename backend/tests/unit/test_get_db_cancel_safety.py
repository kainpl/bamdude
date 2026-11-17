"""Tests for `get_db` cancel-safety (#1112).

Ports upstream Bambuddy's `test_get_db_cancel_safety.py` (audit cycle
v0.2.3.2 → v0.2.4b1, item A.11).

Starlette's BaseHTTPMiddleware cancels the inner task scope when a client
disconnects mid-request. Pre-fix `get_db` only caught `Exception` (not
`BaseException`), so `CancelledError` skipped the rollback path — the SQLite
write lock stayed held until the connection was eventually GC'd, producing
the "database is locked" cascade in @Carter3DP's support package on #1112.

The fix:
  1. Catch `BaseException` so `CancelledError` triggers rollback.
  2. `asyncio.shield` rollback + close so the cleanup completes even when
     the await is cancelled by the same cancel scope.

``get_db`` itself is no longer a generator: it enters ``_request_session`` on
FastAPI's function-scope stack, so the transaction ends before the response is
sent (see ``test_get_db_commits_before_the_response.py``). The commit / rollback /
close contract lives in ``_request_session``, which is what these tests drive.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.core import database


class _FakeSession:
    """Minimal async-context-manager stand-in for ``AsyncSession``.

    Records which lifecycle methods were invoked so tests can assert on
    the cleanup order without a real engine / DB file.
    """

    def __init__(self):
        self.commit = AsyncMock(name="commit")
        self.rollback = AsyncMock(name="rollback")
        self.close = AsyncMock(name="close")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False  # don't suppress


@pytest.fixture
def fake_session_factory(monkeypatch):
    """Patch ``database.async_session`` to yield a fresh ``_FakeSession``."""
    session = _FakeSession()
    monkeypatch.setattr(database, "async_session", lambda: session)
    return session


async def _consume_get_db(action):
    """Drive the request's session scope like FastAPI's exit stack does: enter
    it, run ``action(session)``, then leave it. Returns the entered session."""
    async with database._request_session() as session:
        await action(session)
    return session


async def _leave_with(error):
    """Enter the request's session scope and leave it with ``error`` raised inside."""
    async with database._request_session():
        raise error


class TestCancelSafety:
    """Pin the cancel-safety contract end-to-end."""

    @pytest.mark.asyncio
    async def test_commit_on_clean_exit(self, fake_session_factory):
        session = fake_session_factory

        async def noop(_s):
            pass

        await _consume_get_db(noop)

        session.commit.assert_awaited_once()
        session.rollback.assert_not_awaited()
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rollback_on_regular_exception(self, fake_session_factory):
        session = fake_session_factory

        with pytest.raises(ValueError):
            await _leave_with(ValueError("route handler bug"))

        session.commit.assert_not_awaited()
        session.rollback.assert_awaited_once()
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rollback_on_cancelled_error(self, fake_session_factory):
        """The actual #1112 fix: CancelledError must NOT skip the rollback.
        Pre-fix ``except Exception`` caught nothing because CancelledError is
        a BaseException, not an Exception."""
        session = fake_session_factory

        with pytest.raises(asyncio.CancelledError):
            await _leave_with(asyncio.CancelledError("client disconnected"))

        session.commit.assert_not_awaited()
        session.rollback.assert_awaited_once()
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_runs_even_if_rollback_raises(self, fake_session_factory):
        """A failing rollback (broken connection during cancellation) must
        not prevent ``close`` from running — otherwise the pool would never
        reclaim the connection."""
        session = fake_session_factory
        session.rollback.side_effect = OSError("broken pipe during rollback")

        with pytest.raises(asyncio.CancelledError):
            await _leave_with(asyncio.CancelledError())

        session.rollback.assert_awaited_once()
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_failure_does_not_propagate(self, fake_session_factory):
        """A failing close on the clean-exit path must not raise out of
        ``get_db`` — the request already succeeded."""
        session = fake_session_factory
        session.close.side_effect = OSError("close failed")

        async def noop(_s):
            pass

        # Must not raise.
        await _consume_get_db(noop)

        session.commit.assert_awaited_once()
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rollback_uses_shield(self, fake_session_factory):
        """Cancellation arriving DURING rollback must not abort the
        rollback — ``asyncio.shield`` keeps it running. Verify the call path
        goes through ``shield`` so future refactors don't silently drop the
        protection."""
        with (
            patch.object(asyncio, "shield", wraps=asyncio.shield) as shield,
            pytest.raises(asyncio.CancelledError),
        ):
            await _leave_with(asyncio.CancelledError())

        assert shield.call_count == 2
