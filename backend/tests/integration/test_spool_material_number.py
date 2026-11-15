"""The material number — an internal article code on a spool (upstream 71d4b2f70, #2994).

Free text beside «Лот», shared by the spools of one filament: a new spool of an
already-numbered filament arrives with the number, the list sorts, filters and
searches by it on the server, bulk edit and CSV carry it, and consumption is
grouped by it in the statistics.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory

API = "/api/v1/inventory"

BASE = {
    "material": "PLA",
    "subtype": "Basic",
    "brand": "Acme",
    "color_name": "Black",
    "rgba": "000000FF",
    "label_weight": 1000,
}


async def _create(async_client, **fields) -> dict:
    response = await async_client.post(f"{API}/spools", json={**BASE, **fields})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(response) -> list[int]:
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"]]


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheField:
    async def test_it_is_trimmed_and_blank_means_none(self, async_client):
        assert (await _create(async_client, material_number="  15 "))["material_number"] == "15"
        assert (await _create(async_client, color_name="White", material_number="   "))["material_number"] is None
        assert (await _create(async_client, color_name="Red"))["material_number"] is None

    async def test_it_is_bounded(self, async_client):
        response = await async_client.post(f"{API}/spools", json={**BASE, "material_number": "x" * 65})
        assert response.status_code == 422

    async def test_an_edit_sets_and_clears_it(self, async_client):
        spool = await _create(async_client)

        set_ = await async_client.patch(f"{API}/spools/{spool['id']}", json={"material_number": " A-7 "})
        assert set_.json()["material_number"] == "A-7"
        cleared = await async_client.patch(f"{API}/spools/{spool['id']}", json={"material_number": ""})
        assert cleared.json()["material_number"] is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestANewSpoolOfTheSameFilamentInheritsIt:
    async def test_the_same_filament_takes_the_number(self, async_client):
        await _create(async_client, material_number="15")

        assert (await _create(async_client))["material_number"] == "15"
        # Another colour, another size or another brand is another filament.
        assert (await _create(async_client, color_name="White", rgba="FFFFFFFF"))["material_number"] is None
        assert (await _create(async_client, label_weight=250))["material_number"] is None
        assert (await _create(async_client, brand="Other"))["material_number"] is None

    async def test_a_number_the_caller_gave_wins(self, async_client):
        await _create(async_client, material_number="15")

        assert (await _create(async_client, material_number="99"))["material_number"] == "99"

    async def test_an_archived_spool_still_names_its_filament(self, async_client, db_session):
        source = await _create(async_client, material_number="15")
        await async_client.post(f"{API}/spools/{source['id']}/archive")

        assert (await _create(async_client))["material_number"] == "15"

    async def test_the_most_recently_touched_spool_decides(self, async_client, db_session):
        older = await _create(async_client, material_number="15")
        newer = await _create(async_client, material_number="16")
        rows = {s.id: s for s in (await db_session.execute(select(Spool))).scalars()}
        rows[older["id"]].updated_at = datetime(2026, 1, 1)
        rows[newer["id"]].updated_at = datetime(2026, 2, 1)
        await db_session.commit()

        assert (await _create(async_client))["material_number"] == "16"

    async def test_a_bundle_inherits_too(self, async_client):
        await _create(async_client, material_number="15")

        response = await async_client.post(f"{API}/spools/bulk", json={"spool": BASE, "quantity": 3})

        assert response.status_code == 200, response.text
        assert [row["material_number"] for row in response.json()] == ["15", "15", "15"]

    async def test_an_edit_never_inherits(self, async_client):
        await _create(async_client, material_number="15")
        other = await _create(async_client, color_name="White", rgba="FFFFFFFF")

        # Now made the same filament by an edit — its number stays its own.
        edited = await async_client.patch(
            f"{API}/spools/{other['id']}", json={"color_name": "Black", "rgba": "000000FF"}
        )
        assert edited.json()["material_number"] is None

    async def test_a_tag_read_spool_inherits(self, db_session):
        from backend.app.services import inventory_service

        db_session.add(Spool(**BASE, material_number="15"))
        await db_session.commit()
        fresh = Spool(**BASE)

        await inventory_service.inherit_material_number(db_session, fresh)

        assert fresh.material_number == "15"


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheList:
    async def _seed(self, async_client) -> dict[str, int]:
        a = await _create(async_client, material_number="15")
        b = await _create(async_client, color_name="White", rgba="FFFFFFFF", material_number="A-7")
        c = await _create(async_client, color_name="Red", rgba="FF0000FF")
        return {"15": a["id"], "A-7": b["id"], "none": c["id"]}

    async def test_a_filter_and_its_facet(self, async_client):
        ids = await self._seed(async_client)
        page = f"{API}/spools?page=1&per_page=50"

        assert _ids(await async_client.get(f"{page}&material_number=15")) == [ids["15"]]
        assert _ids(await async_client.get(f"{page}&material_number=__none__")) == [ids["none"]]
        facets = (await async_client.get(f"{API}/spools/facets")).json()
        assert facets["material_numbers"] == ["15", "A-7"]
        # The "select all matching" feed reads the same filter.
        selected = await async_client.get(f"{API}/spools/ids?material_number=A-7")
        assert selected.json()["ids"] == [ids["A-7"]]

    async def test_a_server_sort(self, async_client):
        ids = await self._seed(async_client)
        page = f"{API}/spools?page=1&per_page=50"

        ascending = _ids(await async_client.get(f"{page}&sort_by=material_number_asc"))
        descending = _ids(await async_client.get(f"{page}&sort_by=material_number_desc"))

        assert ascending == [ids["none"], ids["15"], ids["A-7"]]
        assert descending == [ids["A-7"], ids["15"], ids["none"]]

    async def test_search_finds_it(self, async_client):
        ids = await self._seed(async_client)

        assert _ids(await async_client.get(f"{API}/spools?page=1&per_page=50&q=A-7")) == [ids["A-7"]]

    async def test_it_is_a_level_of_the_operators_spool_order(self):
        from backend.app.services import inventory_service

        assert "material_number" in inventory_service.SPOOL_ORDER_KEYS
        assert inventory_service.parse_spool_sort_order("material_number_asc,id_desc") == [
            ("material_number", "asc"),
            ("id", "desc"),
        ]

    async def test_bulk_edit_writes_it_to_the_selection_only(self, async_client):
        ids = await self._seed(async_client)

        response = await async_client.patch(
            f"{API}/spools/bulk-update",
            json={"spool_ids": [ids["15"], ids["none"]], "fields": {"material_number": " 21 "}},
        )

        assert response.status_code == 200, response.text
        numbers = {row["id"]: row["material_number"] for row in (await async_client.get(f"{API}/spools")).json()}
        assert numbers == {ids["15"]: "21", ids["none"]: "21", ids["A-7"]: "A-7"}


@pytest.mark.asyncio
@pytest.mark.integration
class TestCsv:
    async def test_export_and_import_carry_it_and_the_purchase_location(self, async_client, db_session):
        await _create(async_client, material_number="15", purchase_location="Local shop")

        exported = await async_client.get(f"{API}/spools/export")
        assert exported.status_code == 200
        header, row = exported.text.strip().splitlines()[:2]
        columns = header.split(",")
        assert "material_number" in columns and "purchase_location" in columns
        cells = dict(zip(columns, row.split(","), strict=True))
        assert (cells["material_number"], cells["purchase_location"]) == ("15", "Local shop")

        csv_text = "material,brand,color_name,material_number,purchase_location\nPETG,Acme,Blue,P-3,Market\n"
        imported = await async_client.post(
            f"{API}/spools/import", files={"file": ("spools.csv", csv_text.encode(), "text/csv")}
        )
        assert imported.status_code == 200, imported.text
        petg = (await db_session.execute(select(Spool).where(Spool.material == "PETG"))).scalars().one()
        assert (petg.material_number, petg.purchase_location) == ("P-3", "Market")

    async def test_a_blank_cell_states_no_number(self, async_client, db_session):
        """The file is authoritative: an import never guesses a number from the
        rest of the inventory."""
        await _create(async_client, material_number="15")

        csv_text = "material,brand,subtype,color_name,rgba,label_weight\nPLA,Acme,Basic,Black,000000FF,1000\n"
        imported = await async_client.post(
            f"{API}/spools/import", files={"file": ("spools.csv", csv_text.encode(), "text/csv")}
        )

        assert imported.status_code == 200, imported.text
        numbers = sorted(str(n) for n in (await db_session.execute(select(Spool.material_number))).scalars())
        assert numbers == ["15", "None"]


@pytest.mark.asyncio
@pytest.mark.integration
class TestNamesAndLabels:
    async def test_the_naming_template_and_the_label_can_show_it(self, db_session):
        from backend.app.services import label_context
        from backend.app.services.label_template import NAMING_PLACEHOLDERS

        assert "material_number" in {p.key for p in NAMING_PLACEHOLDERS}
        spool = Spool(id=4, **BASE, material_number="15")
        assert label_context.spool_context(spool, deeplink_base="http://x")["material_number"] == "15"
        assert label_context.spool_context(Spool(id=5, **BASE), deeplink_base="http://x")["material_number"] == ""

    async def test_a_template_with_it_is_searchable(self, async_client, db_session):
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="spool_display_template", value="[{material_number}] {brand}"))
        await db_session.commit()
        spool = await _create(async_client, material_number="15")

        assert _ids(await async_client.get(f"{API}/spools?page=1&per_page=50&q=[15]")) == [spool["id"]]


@pytest.mark.asyncio
@pytest.mark.integration
class TestStatisticsByMaterialNumber:
    async def test_consumption_and_cost_are_grouped_by_it(self, async_client, db_session):
        a = await _create(async_client, material_number="15")
        b = await _create(async_client, color_name="White", rgba="FFFFFFFF", material_number="15")
        c = await _create(async_client, color_name="Red", rgba="FF0000FF", material_number="A-7")
        d = await _create(async_client, color_name="Blue", rgba="0000FFFF")  # no number
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db_session.add_all(
            [
                SpoolUsageHistory(spool_id=a["id"], weight_used=100.0, cost=2.5, created_at=now),
                SpoolUsageHistory(spool_id=b["id"], weight_used=50.0, cost=1.0, created_at=now),
                SpoolUsageHistory(spool_id=c["id"], weight_used=30.0, cost=None, created_at=now),
                SpoolUsageHistory(spool_id=d["id"], weight_used=999.0, cost=9.0, created_at=now),
                SpoolUsageHistory(spool_id=a["id"], weight_used=400.0, cost=10.0, created_at=now - timedelta(days=40)),
            ]
        )
        await db_session.commit()

        today = datetime.now(timezone.utc).date()
        ranged = await async_client.get(
            "/api/v1/statistics/aggregate",
            params={"date_from": str(today - timedelta(days=7)), "date_to": str(today + timedelta(days=1))},
        )
        assert ranged.status_code == 200, ranged.text
        assert ranged.json()["by_material_number"] == [
            {"material_number": "15", "grams": 150.0, "cost": 3.5, "spools": 2},
            {"material_number": "A-7", "grams": 30.0, "cost": 0.0, "spools": 1},
        ]

        all_time = (await async_client.get("/api/v1/statistics/aggregate")).json()["by_material_number"]
        assert all_time[0] == {"material_number": "15", "grams": 550.0, "cost": 13.5, "spools": 2}

    async def test_a_weight_correction_is_not_a_prints_consumption(self, async_client, db_session):
        """An AMS weight sync or a runout tail corrects the spool's own ledger: 80 g printed, and the
        120 g correction used to be counted as consumed too (200 g) -- every other figure of the page
        leaves such rows out."""
        from backend.app.models.spool_usage_history import AMS_SYNC_STATUS, RUNOUT_STATUS

        spool = await _create(async_client, material_number="15")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db_session.add_all(
            [
                SpoolUsageHistory(spool_id=spool["id"], weight_used=80.0, cost=2.0, status="completed", created_at=now),
                SpoolUsageHistory(spool_id=spool["id"], weight_used=100.0, status=AMS_SYNC_STATUS, created_at=now),
                SpoolUsageHistory(spool_id=spool["id"], weight_used=20.0, status=RUNOUT_STATUS, created_at=now),
            ]
        )
        await db_session.commit()

        body = (await async_client.get("/api/v1/statistics/aggregate")).json()

        assert body["by_material_number"] == [{"material_number": "15", "grams": 80.0, "cost": 2.0, "spools": 1}]


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpoolmanMode:
    async def test_the_filaments_article_number_is_the_material_number(self):
        from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool

        spool = {
            "id": 3,
            "remaining_weight": 800,
            "used_weight": 200,
            "filament": {"id": 7, "name": "PLA Basic", "material": "PLA", "weight": 1000, "article_number": " 15 "},
        }
        assert _map_spoolman_spool(spool)["material_number"] == "15"
        spool["filament"].pop("article_number")
        assert _map_spoolman_spool(spool)["material_number"] is None
