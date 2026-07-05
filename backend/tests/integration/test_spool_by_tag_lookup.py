"""Integration tests for GET /inventory/spools/by-tag (upstream Bambuddy #1663).

The route lets NFC inventory integrations look a spool up by its ``tray_uuid``
(primary) or ``tag_uid`` (fallback) without listing the whole inventory. Matching
is normalized: the query is hex-normalized + upper-cased and compared against
``func.upper(column)``, so case/format differences still match. It must be
declared before ``/spools/{spool_id}`` so the literal path isn't captured by the
integer-param route.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool

_TRAY_UUID = "0123456789ABCDEF0123456789ABCDEF"
_TAG_UID = "FEDCBA9876543210"


@pytest.fixture
async def tagged_spool(db_session: AsyncSession):
    spool = Spool(
        material="PLA",
        subtype="Basic",
        brand="Devil Design",
        color_name="Red",
        rgba="FF0000FF",
        label_weight=1000,
        weight_used=0,
        tray_uuid=_TRAY_UUID,
        tag_uid=_TAG_UID,
    )
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


class TestSpoolByTagLookup:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_lookup_by_tray_uuid(self, async_client: AsyncClient, tagged_spool):
        resp = await async_client.get(f"/api/v1/inventory/spools/by-tag?tray_uuid={_TRAY_UUID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == tagged_spool.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_lookup_by_tag_uid(self, async_client: AsyncClient, tagged_spool):
        resp = await async_client.get(f"/api/v1/inventory/spools/by-tag?tag_uid={_TAG_UID}")
        assert resp.status_code == 200
        assert resp.json()["id"] == tagged_spool.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_lookup_is_normalized(self, async_client: AsyncClient, tagged_spool):
        # Lowercase + colon-separated input must still resolve (hex-normalized + upper-cased).
        messy = "01:23:45:67:89:ab:cd:ef:01:23:45:67:89:ab:cd:ef"
        resp = await async_client.get(f"/api/v1/inventory/spools/by-tag?tray_uuid={messy}")
        assert resp.status_code == 200
        assert resp.json()["id"] == tagged_spool.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_requires_at_least_one_identifier(self, async_client: AsyncClient, tagged_spool):
        resp = await async_client.get("/api/v1/inventory/spools/by-tag")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_not_found(self, async_client: AsyncClient, tagged_spool):
        resp = await async_client.get("/api/v1/inventory/spools/by-tag?tray_uuid=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_literal_path_not_captured_by_spool_id_route(self, async_client: AsyncClient, tagged_spool):
        # If routing were wrong, "by-tag" would hit /spools/{spool_id} and 422 on int parse.
        resp = await async_client.get(f"/api/v1/inventory/spools/by-tag?tag_uid={_TAG_UID}")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archived_excluded_by_default(self, async_client: AsyncClient, db_session: AsyncSession):
        from datetime import datetime

        spool = Spool(
            material="PLA",
            label_weight=1000,
            weight_used=0,
            tray_uuid="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            archived_at=datetime(2024, 1, 1),
        )
        db_session.add(spool)
        await db_session.commit()

        resp = await async_client.get("/api/v1/inventory/spools/by-tag?tray_uuid=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
        assert resp.status_code == 404

        resp2 = await async_client.get(
            "/api/v1/inventory/spools/by-tag?tray_uuid=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB&include_archived=true"
        )
        assert resp2.status_code == 200
