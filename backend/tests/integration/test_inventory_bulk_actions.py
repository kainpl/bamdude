"""Bulk delete / archive / restore on the built-in inventory (#1795).

Upstream shipped four bulk endpoints per inventory mode; we had only
bulk-update, so archiving or deleting a selection meant one request per row —
and in Spoolman mode there was no bulk edit at all.

The contract these pin: an id that no longer exists must not abort the batch
(another tab can delete a row between the click and the request), a no-op must
be reported as a no-op rather than counted as work done, and an empty selection
must be rejected rather than answered 200 with "0 archived".
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool


async def _make_spool(db: AsyncSession, **kwargs) -> Spool:
    defaults = {"material": "PLA", "color_name": "Red", "rgba": "FF0000FF", "label_weight": 1000, "weight_used": 0}
    defaults.update(kwargs)
    spool = Spool(**defaults)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


class TestBulkDelete:
    @pytest.mark.asyncio
    async def test_deletes_every_listed_spool(self, async_client: AsyncClient, db_session: AsyncSession):
        a = await _make_spool(db_session, brand="A")
        b = await _make_spool(db_session, brand="B")
        keep = await _make_spool(db_session, brand="Keep")

        resp = await async_client.post("/api/v1/inventory/spools/bulk-delete", json={"spool_ids": [a.id, b.id]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] == 2
        assert resp.json()["not_found"] == []

        remaining = (await db_session.execute(select(Spool.id))).scalars().all()
        assert set(remaining) == {keep.id}

    @pytest.mark.asyncio
    async def test_unknown_id_is_reported_not_fatal(self, async_client: AsyncClient, db_session: AsyncSession):
        a = await _make_spool(db_session, brand="A")

        resp = await async_client.post("/api/v1/inventory/spools/bulk-delete", json={"spool_ids": [a.id, 999999]})
        assert resp.status_code == 200, resp.text
        # The real one still went, and the missing one is named rather than
        # silently swallowed or aborting the batch.
        assert resp.json()["deleted"] == 1
        assert resp.json()["not_found"] == [999999]

    @pytest.mark.asyncio
    async def test_empty_selection_is_rejected(self, async_client: AsyncClient):
        resp = await async_client.post("/api/v1/inventory/spools/bulk-delete", json={"spool_ids": []})
        assert resp.status_code == 422


class TestBulkArchiveRestore:
    @pytest.mark.asyncio
    async def test_archive_sets_archived_at_and_skips_already_archived(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        from datetime import datetime, timezone

        fresh = await _make_spool(db_session, brand="Fresh")
        already = await _make_spool(db_session, brand="Already", archived_at=datetime.now(timezone.utc))

        resp = await async_client.post(
            "/api/v1/inventory/spools/bulk-archive", json={"spool_ids": [fresh.id, already.id]}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Counted apart so the UI can say "1 archived, 1 already was" rather
        # than claiming it archived two.
        assert body["archived"] == 1
        assert body["already_archived"] == 1

        await db_session.refresh(fresh)
        assert fresh.archived_at is not None

    @pytest.mark.asyncio
    async def test_restore_is_the_inverse(self, async_client: AsyncClient, db_session: AsyncSession):
        from datetime import datetime, timezone

        archived = await _make_spool(db_session, brand="Arch", archived_at=datetime.now(timezone.utc))
        active = await _make_spool(db_session, brand="Active")

        resp = await async_client.post(
            "/api/v1/inventory/spools/bulk-restore", json={"spool_ids": [archived.id, active.id]}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["restored"] == 1
        assert body["already_active"] == 1

        await db_session.refresh(archived)
        assert archived.archived_at is None

    @pytest.mark.asyncio
    async def test_archive_reports_unknown_ids(self, async_client: AsyncClient, db_session: AsyncSession):
        a = await _make_spool(db_session, brand="A")
        resp = await async_client.post("/api/v1/inventory/spools/bulk-archive", json={"spool_ids": [a.id, 999999]})
        assert resp.status_code == 200
        assert resp.json()["not_found"] == [999999]


class TestLiteralPathsBeatTheIntMatcher:
    """`/spools/bulk-*` must not be captured by `/spools/{spool_id}`."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["bulk-delete", "bulk-archive", "bulk-restore"])
    async def test_bulk_paths_are_routed(self, async_client: AsyncClient, action: str):
        # A 422 (empty list) proves the literal route matched and validated;
        # a 404/405 would mean it fell through to the {spool_id} matcher.
        resp = await async_client.post(f"/api/v1/inventory/spools/{action}", json={"spool_ids": []})
        assert resp.status_code == 422
