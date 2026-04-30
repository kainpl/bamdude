"""Integration tests for archive auto-purge (#1008 follow-up)."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.integration
async def test_settings_defaults_when_unset(async_client: AsyncClient):
    """GET /archives/purge/settings returns sensible defaults on a fresh install."""
    resp = await async_client.get("/api/v1/archives/purge/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["days"] == 365


@pytest.mark.asyncio
@pytest.mark.integration
async def test_settings_roundtrip(async_client: AsyncClient):
    """PUT persists, GET returns the saved values, days is clamped."""
    resp = await async_client.put(
        "/api/v1/archives/purge/settings",
        json={"enabled": True, "days": 180},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": True, "days": 180}

    resp = await async_client.get("/api/v1/archives/purge/settings")
    assert resp.json() == {"enabled": True, "days": 180}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_settings_rejects_out_of_range_days(async_client: AsyncClient):
    """days below MIN or above MAX is rejected."""
    resp = await async_client.put(
        "/api/v1/archives/purge/settings",
        json={"enabled": True, "days": 1},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_preview_counts_old_archives(async_client: AsyncClient, archive_factory, printer_factory, db_session):
    """Preview returns the count + total bytes of archives older than the threshold."""
    printer = await printer_factory()
    old = await archive_factory(printer.id, print_name="Old", file_size=1000)
    fresh = await archive_factory(printer.id, print_name="Fresh", file_size=2000)

    old.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    fresh.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    await db_session.commit()

    resp = await async_client.get("/api/v1/archives/purge/preview?older_than_days=365")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["total_bytes"] == 1000


@pytest.mark.asyncio
@pytest.mark.integration
async def test_preview_ignores_recently_reprinted_archives(
    async_client: AsyncClient, archive_factory, printer_factory, db_session
):
    """Reprints update completed_at but leave created_at pinned; purge must honour that."""
    printer = await printer_factory()
    reprinted = await archive_factory(printer.id, print_name="Reprinted", file_size=1000)

    # Originally printed 400 days ago, but a reprint last week refreshed completed_at.
    reprinted.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    reprinted.started_at = datetime.now(timezone.utc) - timedelta(days=7)
    reprinted.completed_at = datetime.now(timezone.utc) - timedelta(days=7)
    await db_session.commit()

    resp = await async_client.get("/api/v1/archives/purge/preview?older_than_days=365")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_manual_purge_moves_old_archives_to_trash(
    async_client: AsyncClient, archive_factory, printer_factory, db_session
):
    """POST /archives/purge moves archives older than the threshold to trash.

    Behaviour changed from hard-delete to soft-delete (#1008 follow-up): the
    sweeper hard-deletes after retention, users can restore in the meantime.
    """
    from backend.app.models.archive import PrintArchive

    printer = await printer_factory()
    old = await archive_factory(printer.id, print_name="Old")
    fresh = await archive_factory(printer.id, print_name="Fresh")

    old_id = old.id
    fresh_id = fresh.id
    old.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    fresh.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    await db_session.commit()

    resp = await async_client.post(
        "/api/v1/archives/purge",
        json={"older_than_days": 365},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved_to_trash"] == 1

    db_session.expire_all()
    # Old archive went to trash (deleted_at stamped); fresh untouched.
    old_row = await db_session.get(PrintArchive, old_id)
    fresh_row = await db_session.get(PrintArchive, fresh_id)
    assert old_row is not None and old_row.deleted_at is not None
    assert fresh_row is not None and fresh_row.deleted_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_auto_purge_runs_when_enabled(async_client: AsyncClient, archive_factory, printer_factory, db_session):
    """With the toggle on, a stale archive is soft-deleted (moved to trash).

    Behaviour changed (#1008 follow-up): auto-purge now stamps ``deleted_at``
    instead of hard-deleting. The trash sweeper hard-deletes after retention.
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.services.archive_purge import archive_purge_service

    printer = await printer_factory()
    stale = await archive_factory(printer.id, print_name="Stale")
    stale_id = stale.id
    stale.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    await db_session.commit()

    await archive_purge_service.set_settings(db_session, enabled=True, days=365)

    moved = await archive_purge_service._maybe_run_auto_purge(db_session)
    assert moved >= 1

    db_session.expire_all()
    row = await db_session.get(PrintArchive, stale_id)
    assert row is not None and row.deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_auto_purge_throttles_within_24h(async_client: AsyncClient, archive_factory, printer_factory, db_session):
    """A recent last-run timestamp blocks the sweeper for 24h."""
    from backend.app.services.archive_purge import archive_purge_service

    printer = await printer_factory()
    stale = await archive_factory(printer.id, print_name="Stale")
    stale.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    await db_session.commit()

    await archive_purge_service.set_settings(db_session, enabled=True, days=365)
    await archive_purge_service._stamp_last_run(db_session, datetime.now(timezone.utc) - timedelta(hours=1))

    moved = await archive_purge_service._maybe_run_auto_purge(db_session)
    assert moved == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_auto_purge_skipped_when_disabled(
    async_client: AsyncClient, archive_factory, printer_factory, db_session
):
    """When the toggle is off, old archives stay put."""
    from backend.app.models.archive import PrintArchive
    from backend.app.services.archive_purge import archive_purge_service

    printer = await printer_factory()
    stale = await archive_factory(printer.id, print_name="Stale")
    stale_id = stale.id
    stale.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    await db_session.commit()

    await archive_purge_service.set_settings(db_session, enabled=False, days=365)
    moved = await archive_purge_service._maybe_run_auto_purge(db_session)
    assert moved == 0

    db_session.expire_all()
    row = await db_session.get(PrintArchive, stale_id)
    assert row is not None and row.deleted_at is None
