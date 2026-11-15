"""The History view and the spool dialog's usage list in Spoolman mode (m228, upstream 1d86cff5a).

The rows are served from their own table, fenced to the current Spoolman, and
never beside the built-in inventory's rows — neither mode shows the other's
history (upstream 704919339: a Spoolman spool whose id equalled a built-in
spool's took over its prints).
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory
from backend.app.services import spoolman_usage_history as history

REGISTERED = "2026-10-01T08:00:00Z"
BASE = "/api/v1/spoolman/inventory"


def _spool(spool_id, *, registered=REGISTERED, price=20.0, material="PLA", name="PLA Matte", vendor="Sunlu"):
    return {
        "id": spool_id,
        "registered": registered,
        "price": price,
        "initial_weight": 1000,
        "used_weight": 0,
        "remaining_weight": 1000,
        "archived": False,
        "filament": {
            "name": name,
            "material": material,
            "color_hex": "112233",
            "weight": 1000,
            "vendor": {"name": vendor},
        },
        "extra": {},
    }


@pytest.fixture
async def spoolman_mode(db_session):
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
    await db_session.commit()


@pytest.fixture
def spoolman_client():
    client = MagicMock()
    client.base_url = "http://localhost:7912"
    client.health_check = AsyncMock(return_value=True)
    client.spools = {5: _spool(5), 6: _spool(6, material="PETG", name="PETG HF", vendor="Bambu Lab")}

    async def _get_spool(spool_id):
        return client.spools[spool_id]

    client.get_spool = AsyncMock(side_effect=_get_spool)
    with (
        patch("backend.app.api.routes.spoolman_inventory.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman_inventory.init_spoolman_client", AsyncMock(return_value=client)),
    ):
        yield client


async def _charge(spool, grams, *, status="completed", rate=0.02, printer_id=None):
    await history.record_print(
        printer_id=printer_id,
        archive_id=None,
        status=status,
        charges=[history.Charge(spool_id=spool["id"], grams=grams, rate=rate, spool=spool)],
    )


async def _age(db_session, spool_id, days):
    """Move a spool's rows back in time (the writer stamps now)."""
    rows = (
        (
            await db_session.execute(
                select(SpoolmanUsageHistory).where(SpoolmanUsageHistory.spoolman_spool_id == spool_id)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.created_at = row.created_at - timedelta(days=days)
    await db_session.commit()


class TestTheHistoryFeed:
    @pytest.mark.asyncio
    async def test_a_page_carries_the_spool_as_it_was_and_totals_of_the_whole_filter(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 100.0)
        await _charge(spoolman_client.spools[6], 40.0, status="failed", rate=None)
        await _charge(spoolman_client.spools[5], 10.0)

        response = await async_client.get(f"{BASE}/usage", params={"per_page": 2, "sort_by": "weight_used_desc"})
        assert response.status_code == 200
        body = response.json()
        assert [i["weight_used"] for i in body["items"]] == [100.0, 40.0]
        assert body["meta"] == {"total": 3, "current_page": 1, "per_page": 2, "last_page": 2}
        assert body["totals"] == {"weight_used": 150.0, "cost": 2.2}
        first = body["items"][0]
        assert first["spool_id"] == 5
        assert (first["spool"]["material"], first["spool"]["subtype"], first["spool"]["brand"]) == (
            "PLA",
            "Matte",
            "Sunlu",
        )
        assert body["items"][1]["cost"] is None

    @pytest.mark.asyncio
    async def test_search_filters_and_facets_are_answered_by_the_server(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 100.0)
        await _charge(spoolman_client.spools[6], 40.0, status="failed")

        found = (await async_client.get(f"{BASE}/usage", params={"q": "bambu petg"})).json()
        assert [i["spool_id"] for i in found["items"]] == [6]
        failed = (await async_client.get(f"{BASE}/usage", params={"status": "failed"})).json()
        assert [i["spool_id"] for i in failed["items"]] == [6]
        by_material = (await async_client.get(f"{BASE}/usage", params={"material": "PLA"})).json()
        assert [i["spool_id"] for i in by_material["items"]] == [5]

        facets = (await async_client.get(f"{BASE}/usage/facets")).json()
        assert facets == {
            "statuses": ["completed", "failed"],
            "printers": [],
            "materials": ["PETG", "PLA"],
            "brands": ["Bambu Lab", "Sunlu"],
        }

    @pytest.mark.asyncio
    async def test_the_feed_needs_no_spoolman_round_trip(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 100.0)
        spoolman_client.get_spool.reset_mock()
        spoolman_client.health_check.reset_mock()
        assert (await async_client.get(f"{BASE}/usage")).status_code == 200
        spoolman_client.get_spool.assert_not_called()
        spoolman_client.health_check.assert_not_called()


class TestNeitherInventoryShowsTheOthersHistory:
    @pytest.mark.asyncio
    async def test_a_spoolman_spool_does_not_take_over_a_built_in_spools_prints(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        """Upstream 704919339: built-in spool 5 was used before Spoolman was switched on."""
        local = Spool(id=5, material="ABS", label_weight=1000, weight_used=300)
        db_session.add(local)
        db_session.add(SpoolUsageHistory(spool_id=5, weight_used=300, percent_used=30, status="completed"))
        await db_session.commit()
        await _charge(spoolman_client.spools[5], 12.0)

        feed = (await async_client.get(f"{BASE}/usage")).json()
        assert [(i["spool_id"], i["weight_used"]) for i in feed["items"]] == [(5, 12.0)]
        own = (await async_client.get(f"{BASE}/spools/5/usage")).json()
        assert [r["weight_used"] for r in own] == [12.0]

    @pytest.mark.asyncio
    async def test_the_built_in_feed_never_returns_spoolman_rows(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 12.0)
        feed = (await async_client.get("/api/v1/inventory/usage", params={"page": 1})).json()
        assert feed["items"] == [] and feed["totals"]["weight_used"] == 0

    @pytest.mark.asyncio
    async def test_every_door_refuses_while_the_built_in_inventory_is_on(
        self, async_client: AsyncClient, db_session, spoolman_client
    ):
        db_session.add(Settings(key="spoolman_enabled", value="false"))
        db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
        await db_session.commit()
        for method, path in (
            ("get", "/usage"),
            ("get", "/usage/facets"),
            ("get", "/usage/source"),
            ("get", "/spools/5/usage"),
            ("delete", "/spools/5/usage"),
            ("delete", "/spools/5/usage/1"),
        ):
            response = await getattr(async_client, method)(f"{BASE}{path}")
            assert response.status_code == 400, path


class TestWhichSpoolmanAndWhichSpool:
    @pytest.mark.asyncio
    async def test_a_changed_address_hides_the_old_rows_until_the_operator_keeps_them(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 100.0)
        row = (await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
        row.value = "http://new-host:7912"
        await db_session.commit()
        spoolman_client.base_url = "http://new-host:7912"

        assert (await async_client.get(f"{BASE}/usage")).json()["items"] == []
        state = (await async_client.get(f"{BASE}/usage/source")).json()
        assert (state["rows"], state["previous_rows"], state["previous_url"]) == (0, 1, "http://localhost:7912")

        kept = (await async_client.post(f"{BASE}/usage/source/keep-previous")).json()
        assert (kept["rows"], kept["previous_rows"]) == (1, 0)
        assert len((await async_client.get(f"{BASE}/usage")).json()["items"]) == 1

    @pytest.mark.asyncio
    async def test_a_new_database_behind_the_same_address_starts_over(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 100.0)
        state = (await async_client.post(f"{BASE}/usage/source/rotate")).json()
        assert (state["rows"], state["previous_rows"]) == (0, 1)
        assert (await async_client.get(f"{BASE}/usage")).json()["items"] == []

    @pytest.mark.asyncio
    async def test_a_spool_that_took_over_an_id_does_not_inherit_its_rows(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 100.0)
        await _age(db_session, 5, days=20)
        # Spool 5 is deleted in Spoolman's own UI and the id handed to a new spool.
        spoolman_client.spools[5] = _spool(5, registered=(datetime.utcnow() - timedelta(days=3)).isoformat() + "Z")
        await _charge(spoolman_client.spools[5], 7.0)

        own = (await async_client.get(f"{BASE}/spools/5/usage")).json()
        assert [r["weight_used"] for r in own] == [7.0]
        # The History view still shows both: it is the record of what was burned.
        assert len((await async_client.get(f"{BASE}/usage")).json()["items"]) == 2


class TestForgettingHistoryNeverTouchesSpoolman:
    @pytest.mark.asyncio
    async def test_clearing_a_spool_forgets_only_its_own_rows(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 100.0)
        await _charge(spoolman_client.spools[5], 20.0)
        await _charge(spoolman_client.spools[6], 40.0)

        response = await async_client.delete(f"{BASE}/spools/5/usage")
        assert response.json() == {"status": "cleared", "deleted": 2}
        left = (await db_session.execute(select(SpoolmanUsageHistory.spoolman_spool_id))).scalars().all()
        assert left == [6]
        written = [c for c in spoolman_client.method_calls if c[0] not in ("get_spool", "health_check")]
        assert written == [], "Spoolman was only read"

    @pytest.mark.asyncio
    async def test_one_row_can_be_forgotten_and_a_wrong_pair_is_not_found(
        self, async_client: AsyncClient, db_session, spoolman_mode, spoolman_client
    ):
        await _charge(spoolman_client.spools[5], 100.0)
        row_id = (await db_session.execute(select(SpoolmanUsageHistory.id))).scalar_one()

        assert (await async_client.delete(f"{BASE}/spools/6/usage/{row_id}")).status_code == 404
        assert (await async_client.delete(f"{BASE}/spools/5/usage/{row_id}")).json() == {"status": "deleted"}
        assert (await db_session.execute(select(SpoolmanUsageHistory.id))).scalars().all() == []
