"""B: edits cannot carry a retained credential to a new destination."""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.credential_config import (
    CONFIG_GUARD_KEY,
    canonical_url,
    initialize_credential_config_anchor,
    lock_credential_config,
    merge_secret_fields,
)
from backend.app.core.database import configure_sqlite_connection
from backend.app.migrations import m219_credential_config_guard as migration
from backend.app.models.settings import Settings


def _scope(config):
    return canonical_url(config.get("url"), default="https://example.invalid/api")


def test_changed_destination_requires_fresh_credential_even_on_full_replacement():
    stored = {"url": "https://old.invalid/api", "password": "private", "discarded": "old"}
    with pytest.raises(HTTPException) as error:
        merge_secret_fields(stored, {"url": "https://new.invalid/api"}, {"password"}, replace_config=True, scope=_scope)
    assert error.value.status_code == 400
    assert "private" not in error.value.detail
    updated = merge_secret_fields(
        stored,
        {"url": "https://new.invalid/api", "password": "new*credential"},
        {"password"},
        {"password": "replace"},
        replace_config=True,
        scope=_scope,
    )
    assert updated == {"url": "https://new.invalid/api", "password": "new*credential"}


def test_default_port_and_host_case_are_noops_but_base_path_and_query_are_not():
    stored = {"url": "https://EXAMPLE.invalid:443/api/", "password": "private"}
    assert (
        merge_secret_fields(stored, {"url": "https://example.invalid/api"}, {"password"}, scope=_scope)["password"]
        == "private"
    )
    for url in ("http://example.invalid/api", "https://example.invalid/other", "https://example.invalid/api?q=other"):
        with pytest.raises(HTTPException):
            merge_secret_fields(stored, {"url": url}, {"password"}, scope=_scope)


def test_malformed_legacy_destination_stays_comparable_without_exposing_credential():
    stored = {"url": 123, "password": "private"}
    assert merge_secret_fields(stored, {"label": "Rename"}, {"password"}, scope=_scope)["password"] == "private"
    with pytest.raises(HTTPException) as error:
        merge_secret_fields(stored, {"url": "https://new.invalid"}, {"password"}, scope=_scope)
    assert error.value.status_code == 400
    assert "private" not in error.value.detail


@pytest.mark.parametrize(
    "actions,changes",
    [
        ({"url": "keep"}, {}),
        ({"password": "unknown"}, {}),
        ({"password": "keep"}, {"password": "private"}),
        ({"password": "clear"}, {"password": None}),
        ({"password": "replace"}, {}),
        ({"password": "replace"}, {"password": ""}),
    ],
)
def test_action_conflicts_are_safe_validation_errors(actions, changes):
    with pytest.raises(HTTPException) as error:
        merge_secret_fields({"password": "private"}, changes, {"password"}, actions)
    assert error.value.status_code == 422
    assert "private" not in error.value.detail


def test_legacy_smtp_blanks_keep_but_explicit_clear_removes():
    stored = {"password": "private", "url": "https://old.invalid"}
    for value in (None, ""):
        assert (
            merge_secret_fields(stored, {"password": value}, {"password"}, legacy_blank_keep={"password"})["password"]
            == "private"
        )
        with pytest.raises(HTTPException):
            merge_secret_fields(
                stored,
                {"password": value, "url": "https://new.invalid"},
                {"password"},
                legacy_blank_keep={"password"},
                scope=_scope,
            )
    assert merge_secret_fields(stored, {}, {"password"}, {"password": "clear"})["password"] == ""


@pytest.mark.parametrize("creating,stored", [(True, {"password": "private"}), (False, {})])
def test_explicit_keep_requires_an_existing_stored_credential(creating, stored):
    with pytest.raises(HTTPException) as error:
        merge_secret_fields(stored, {}, {"password"}, {"password": "keep"}, creating=creating)
    assert error.value.status_code == 400


async def test_missing_anchor_fails_closed_without_recreating_it(db_session):
    await db_session.execute(delete(Settings).where(Settings.key == CONFIG_GUARD_KEY))
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await lock_credential_config(db_session)
    assert error.value.status_code == 503
    await db_session.rollback()
    assert await db_session.scalar(select(Settings.id).where(Settings.key == CONFIG_GUARD_KEY)) is None


async def assert_anchor_lifecycle_and_serialization(engine):
    """Two independent connections, no mocked SQL and no real outbound request."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await migration.upgrade(conn)
        await migration.upgrade(conn)
        await initialize_credential_config_anchor(conn)
    async with sessions() as db:
        assert len((await db.scalars(select(Settings).where(Settings.key == CONFIG_GUARD_KEY))).all()) == 1

    # Neither destination nor credential row exists when the first writer
    # starts. The second writes a different key; per-key locks would not help.
    acquired = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def destination_writer():
        async with sessions() as db:
            await lock_credential_config(db)
            await lock_credential_config(db)  # same multi-pair transaction
            db.add(Settings(key="test_destination", value="https://new.invalid/api"))
            await db.flush()
            acquired.set()
            await release.wait()
            await db.commit()

    async def credential_writer():
        await acquired.wait()
        async with sessions() as db:
            started.set()
            await lock_credential_config(db)
            observed.append(await db.scalar(select(Settings.value).where(Settings.key == "test_destination")))
            db.add(Settings(key="test_credential", value="fresh-for-authorized-destination"))
            await db.commit()

    first = asyncio.create_task(destination_writer())
    second = asyncio.create_task(credential_writer())
    try:
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.sleep(0.1)
        assert not second.done(), "disjoint writer passed the installation anchor"
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert observed == ["https://new.invalid/api"]
    finally:
        release.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    held = asyncio.Event()

    async def cancelled_editor():
        async with sessions() as db:
            await lock_credential_config(db)
            held.set()
            await asyncio.Event().wait()

    cancelled = asyncio.create_task(cancelled_editor())
    try:
        await asyncio.wait_for(held.wait(), 5)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        async with sessions() as db:
            await asyncio.wait_for(lock_credential_config(db), 5)
            await db.rollback()
    finally:
        if not cancelled.done():
            cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)

    # A full restore/bootstrap may recreate the missing row before admission.
    async with engine.begin() as conn:
        await conn.execute(delete(Settings).where(Settings.key == CONFIG_GUARD_KEY))
        await initialize_credential_config_anchor(conn)
    async with sessions() as db:
        await lock_credential_config(db)
        await db.rollback()


async def test_absent_disjoint_settings_serialize_on_file_sqlite(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'credentials.db').as_posix()}")
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Settings.__table__.create)
        await assert_anchor_lifecycle_and_serialization(engine)
    finally:
        await engine.dispose()
