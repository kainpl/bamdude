"""Disposable independent connections for queue transaction tests.

An explicit PostgreSQL target uses a unique schema; default is temporary SQLite.
"""

import asyncio
import os
import weakref
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, SessionTransaction

from backend.app.core.config import settings
from backend.app.core.credential_config import initialize_credential_config_anchor
from backend.app.core.database import Base, _strip_tz_from_params, configure_sqlite_connection, import_all_models


@dataclass(frozen=True, eq=False)
class BackendTransaction:
    pid: int
    task: asyncio.Task
    transaction: SessionTransaction


# PostgreSQL: server backend pid -> the active root transaction, which can change even in the same task and pid.
BACKENDS: dict[int, BackendTransaction] = {}
# PostgreSQL: each task -> the task that created it. A writer called through the HTTP client runs its route in a task
# of the middleware's, so the session's task is a descendant of the one the test started.
PARENTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# PostgreSQL transactions currently stopped at a pair's barrier (see ``lane_pairs.hold_until``).
HELD: set[BackendTransaction] = set()


def lineage(task) -> list:
    """``task`` and the tasks that created it, nearest first."""
    chain = []
    while task is not None and all(task is not seen for seen in chain):
        chain.append(task)
        task = PARENTS.get(task)
    return chain


def descends_from(task, ancestor) -> bool:
    return any(each is ancestor for each in lineage(task))


@pytest.fixture
async def test_engine(monkeypatch, tmp_path, request):
    url = os.environ.get("DISPATCH_TEST_POSTGRES_URL", f"sqlite+aiosqlite:///{tmp_path / 'dispatch.sqlite'}")
    schema = "dispatch_" + uuid4().hex if url.startswith("postgresql") else None
    if schema:
        monkeypatch.setattr(settings, "database_url", url)
    options = (
        {
            "connect_args": {
                "server_settings": {
                    "search_path": schema,
                    "timezone": "UTC",
                    "application_name": schema,
                }
            }
        }
        if schema
        else {}
    )
    engine = create_async_engine(url, **options)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    else:
        event.listen(engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)

        # A test that makes one session wait for another holds the first until a monitor has SEEN the wait; a server
        # whose configured lock_timeout is short (the dev server's is 5 s) cancelled the waiter before a monitor on a
        # fresh connection got there. Per transaction (SET LOCAL), so a pooler in transaction mode keeps it to this
        # client; PgBouncer refuses it as a startup parameter. A deadlock is still the server's detector's, a hang the
        # test's own timeout.
        #
        # The same hook names each transaction's server backend after the asyncio task that runs it (BACKENDS), so a
        # pair's monitor can tell ITS waiter and ITS holder from any other backend: a pooler hands a server
        # connection to one transaction at a time, so the task of the latest begin on a pid is the one using it.
        def patient(_session, transaction, connection):
            if connection.engine is engine.sync_engine and transaction.parent is None:
                connection.exec_driver_sql("SET LOCAL lock_timeout = '60s'")
                pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar()
                try:
                    task = asyncio.current_task()
                except RuntimeError:
                    task = None
                if task is not None:
                    BACKENDS[pid] = BackendTransaction(pid, task, transaction)

        def ended(_session, transaction):
            if transaction.parent is None:
                for pid, backend in list(BACKENDS.items()):
                    if backend.transaction is transaction:
                        del BACKENDS[pid]

        BACKENDS.clear()
        HELD.clear()
        event.listen(Session, "after_begin", patient)
        event.listen(Session, "after_transaction_end", ended)
        request.addfinalizer(lambda: event.remove(Session, "after_begin", patient))
        request.addfinalizer(lambda: event.remove(Session, "after_transaction_end", ended))
        request.addfinalizer(BACKENDS.clear)
        request.addfinalizer(HELD.clear)

        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()

        def remembering_parents(loop_, coro, **kwargs):
            task = previous(loop_, coro, **kwargs) if previous is not None else asyncio.Task(coro, loop=loop_, **kwargs)
            creator = asyncio.current_task(loop_)
            if creator is not None:
                PARENTS[task] = creator
            return task

        loop.set_task_factory(remembering_parents)
        request.addfinalizer(lambda: loop.set_task_factory(previous))
    import_all_models()
    async with engine.begin() as connection:
        if schema:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)
        # What ``init_db`` and conftest's engine create: a settings save without it is a 503.
        await initialize_credential_config_anchor(connection)
    monkeypatch.setattr("backend.app.core.database.async_session", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    async with engine.begin() as connection:
        if schema:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        else:
            await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    await asyncio.sleep(0.1)
