"""The Spoolman inventory routes read and write a spool's own numbers
(upstream #2906, #3194, #3195, and the bulk-reset leg of c2bc41a5e).

Driven through the real ``SpoolmanClient`` against ``WeighingSpoolman``, a
double that does Spoolman's weight arithmetic — so "the spool is still half
full after a counter reset" is observed on the Spoolman side, not inferred
from which client method was called.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.tests.unit.services.spoolman_weighing_fake import BASE_URL, WeighingSpoolman, client_for

API = "/api/v1/spoolman/inventory"


@pytest.fixture
async def spoolman_settings(db_session):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value=BASE_URL))
    await db_session.commit()


@pytest.fixture
def spoolman(spoolman_settings):
    """A Spoolman holding one 1000 g catalogue filament of a vendor."""
    from backend.app.api.routes import spoolman_inventory

    fake = WeighingSpoolman()
    fake.add_vendor(3, "Bambu Lab")
    fake.add_filament(7, vendor_id=3, name="PLA Basic", material="PLA", color_hex="FF0000", weight=1000)
    client = client_for(fake)
    spoolman_inventory._health_check_cache.clear()
    with (
        patch("backend.app.api.routes.spoolman_inventory.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman_inventory.init_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
    ):
        yield fake
    spoolman_inventory._health_check_cache.clear()


def _consumed(spool: dict) -> float:
    return spool["weight_used"] - spool["weight_used_baseline"]


@pytest.mark.asyncio
@pytest.mark.integration
class TestResetConsumedCounter:
    async def test_a_reset_leaves_the_spool_half_full_in_spoolman(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, used_weight=456.0)

        response = await async_client.post(f"{API}/spools/42/reset-consumed-counter")

        assert response.status_code == 200
        assert spoolman.view(42)["used_weight"] == 456.0
        assert spoolman.view(42)["remaining_weight"] == 544.0
        body = response.json()
        assert _consumed(body) == 0
        assert body["label_weight"] - body["weight_used"] == 544.0

    async def test_later_use_counts_up_from_zero(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, used_weight=456.0)
        await async_client.post(f"{API}/spools/42/reset-consumed-counter")
        spoolman.spools[42]["used_weight"] = 500.0  # a 44 g print

        response = await async_client.get(f"{API}/spools/42")

        assert _consumed(response.json()) == 44.0

    async def test_an_edit_after_a_reset_keeps_the_remainder(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, used_weight=456.0)
        await async_client.post(f"{API}/spools/42/reset-consumed-counter")

        response = await async_client.patch(f"{API}/spools/42", json={"note": "top shelf"})

        assert response.status_code == 200
        assert spoolman.view(42)["remaining_weight"] == 544.0
        assert _consumed(response.json()) == 0

    async def test_a_bulk_reset_writes_no_weight_either(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, used_weight=456.0)
        spoolman.add_spool(43, 7, used_weight=100.0)

        response = await async_client.post(f"{API}/spools/reset-consumed-counter-bulk", json={"spool_ids": [42, 43]})

        assert response.json() == {"reset": 2, "errors": []}
        assert spoolman.view(42)["remaining_weight"] == 544.0
        assert spoolman.view(43)["remaining_weight"] == 900.0

    async def test_a_bulk_reset_names_the_spools_that_failed(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, used_weight=456.0)

        response = await async_client.post(f"{API}/spools/reset-consumed-counter-bulk", json={"spool_ids": [42, 999]})

        assert response.status_code == 200
        body = response.json()
        assert body["reset"] == 1
        assert [(e["id"], e["status"]) for e in body["errors"]] == [(999, 404)]

    async def test_a_bulk_reset_where_every_spool_failed_says_so(self, async_client: AsyncClient, spoolman):
        response = await async_client.post(f"{API}/spools/reset-consumed-counter-bulk", json={"spool_ids": [998, 999]})

        body = response.json()
        assert body["reset"] == 0
        assert [e["id"] for e in body["errors"]] == [998, 999]


@pytest.mark.asyncio
@pytest.mark.integration
class TestCreate:
    async def test_a_label_weight_is_written_to_the_spool(self, async_client: AsyncClient, spoolman):
        payload = {"spoolman_filament_id": 7, "label_weight": 250, "weight_used": 0}

        response = await async_client.post(f"{API}/spools", json=payload)

        assert response.status_code == 200
        created = spoolman.view(response.json()["id"])
        assert created["initial_weight"] == 250.0
        assert created["remaining_weight"] == 250.0
        assert response.json()["label_weight"] == 250
        assert spoolman.filaments[7]["weight"] == 1000

    async def test_a_caller_that_names_no_size_leaves_it_to_spoolman(self, async_client: AsyncClient, spoolman):
        response = await async_client.post(f"{API}/spools", json={"spoolman_filament_id": 7})

        assert response.status_code == 200
        assert "initial_weight" not in spoolman.bodies("POST", "/spool")[0]

    async def test_the_rate_is_stored_as_the_price_of_that_spool(self, async_client: AsyncClient, spoolman):
        payload = {"spoolman_filament_id": 7, "label_weight": 250, "weight_used": 0, "cost_per_kg": 40}

        response = await async_client.post(f"{API}/spools", json=payload)

        assert spoolman.view(response.json()["id"])["price"] == 10.0
        assert response.json()["cost_per_kg"] == 40.0

    async def test_bulk_create_writes_the_size_and_price_too(self, async_client: AsyncClient, spoolman):
        payload = {
            "spool": {"spoolman_filament_id": 7, "label_weight": 500, "weight_used": 0, "cost_per_kg": 30},
            "quantity": 2,
        }

        response = await async_client.post(f"{API}/spools/bulk", json=payload)

        assert response.status_code == 200
        for created in response.json():
            assert spoolman.view(created["id"])["initial_weight"] == 500.0
            assert spoolman.view(created["id"])["price"] == 15.0


@pytest.mark.asyncio
@pytest.mark.integration
class TestEditLabelWeight:
    async def test_a_resize_goes_to_the_spool_and_leaves_a_shared_filament_alone(
        self, async_client: AsyncClient, spoolman
    ):
        spoolman.add_spool(42, 7, used_weight=50.0)
        spoolman.add_spool(43, 7)

        response = await async_client.patch(f"{API}/spools/42", json={"label_weight": 250, "weight_used": 50})

        assert response.status_code == 200
        assert spoolman.view(42)["initial_weight"] == 250.0
        assert spoolman.view(42)["remaining_weight"] == 200.0
        assert spoolman.view(42)["filament"]["id"] == 7
        assert spoolman.filaments[7]["weight"] == 1000
        assert list(spoolman.filaments) == [7], "no duplicate filament may be minted for a size"
        assert spoolman.view(43)["remaining_weight"] == 1000.0

    async def test_a_resize_of_a_singleton_does_not_resize_its_filament(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7)

        await async_client.patch(f"{API}/spools/42", json={"label_weight": 250, "weight_used": 0})

        assert spoolman.filaments[7]["weight"] == 1000
        assert spoolman.bodies("PATCH", "/filament/7") == []

    async def test_an_edit_of_another_field_keeps_the_spools_own_size(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, initial_weight=250, used_weight=50.0)

        response = await async_client.patch(f"{API}/spools/42", json={"note": "refill"})

        assert response.status_code == 200
        (body,) = spoolman.bodies("PATCH", "/spool/42")
        assert "initial_weight" not in body
        assert spoolman.view(42)["remaining_weight"] == 200.0
        assert spoolman.bodies("PATCH", "/filament/7") == []
        assert list(spoolman.filaments) == [7]

    async def test_a_relink_carries_the_spools_size_to_the_new_filament(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, initial_weight=250, used_weight=50.0)
        spoolman.add_spool(43, 7)  # shared: the edit must relink, not patch

        response = await async_client.patch(f"{API}/spools/42", json={"subtype": "Matte"})

        assert response.status_code == 200
        view = spoolman.view(42)
        assert view["filament"]["id"] != 7
        assert view["initial_weight"] == 250.0
        assert view["remaining_weight"] == 200.0

    async def test_a_spool_without_any_size_takes_the_one_it_is_given(self, async_client: AsyncClient, spoolman):
        spoolman.add_filament(8, vendor_id=3, name="PLA Odd", material="PLA", color_hex="00FF00")
        spoolman.add_spool(44, 8)

        response = await async_client.patch(f"{API}/spools/44", json={"label_weight": 750, "weight_used": 0})

        assert response.status_code == 200
        assert spoolman.view(44)["initial_weight"] == 750.0

    async def test_a_spool_without_any_size_is_not_sent_a_remaining_weight(self, async_client: AsyncClient, spoolman):
        spoolman.add_filament(8, vendor_id=3, name="PLA Odd", material="PLA", color_hex="00FF00")
        spoolman.add_spool(44, 8)

        response = await async_client.patch(f"{API}/spools/44", json={"note": "unknown size"})

        assert response.status_code == 200
        assert "remaining_weight" not in spoolman.bodies("PATCH", "/spool/44")[0]


@pytest.mark.asyncio
@pytest.mark.integration
class TestEditCost:
    async def test_a_rate_is_stored_as_the_price_of_that_spool(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, initial_weight=250)

        response = await async_client.patch(f"{API}/spools/42", json={"cost_per_kg": 40})

        assert spoolman.view(42)["price"] == 10.0
        assert response.json()["cost_per_kg"] == 40.0

    async def test_saving_the_shown_rate_back_does_not_rewrite_the_price(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, initial_weight=250.7, price=7.35)
        shown = (await async_client.get(f"{API}/spools/42")).json()["cost_per_kg"]

        await async_client.patch(f"{API}/spools/42", json={"cost_per_kg": shown, "note": "x"})

        assert "price" not in spoolman.bodies("PATCH", "/spool/42")[0]
        assert spoolman.view(42)["price"] == 7.35

    async def test_a_resize_keeps_the_rate_and_reprices_the_spool(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, price=20)  # 20 per kg on a 1000 g spool

        response = await async_client.patch(f"{API}/spools/42", json={"label_weight": 250, "weight_used": 0})

        assert spoolman.view(42)["price"] == 5.0
        assert response.json()["cost_per_kg"] == 20.0

    async def test_a_spool_priced_through_its_filament_gets_no_price_on_save(self, async_client: AsyncClient, spoolman):
        spoolman.filaments[7]["price"] = 25
        spoolman.add_spool(42, 7)
        listed = (await async_client.get(f"{API}/spools/42")).json()
        assert listed["cost_per_kg"] is None
        assert listed["effective_cost_per_kg"] == 25.0

        await async_client.patch(f"{API}/spools/42", json={"cost_per_kg": listed["cost_per_kg"], "note": "x"})

        assert spoolman.view(42).get("price") is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestWeigh:
    async def test_the_vendors_empty_spool_weight_is_the_tare(self, async_client: AsyncClient, spoolman):
        spoolman.vendors[3]["empty_spool_weight"] = 216
        spoolman.add_spool(42, 7)

        response = await async_client.patch(f"{API}/spools/42/weight", json={"weight_grams": 716})

        assert response.status_code == 200
        assert spoolman.view(42)["remaining_weight"] == 500.0
        assert response.json()["weight_used"] == 500.0

    async def test_the_answer_is_measured_against_the_spools_own_size(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, initial_weight=250, spool_weight=200)

        response = await async_client.patch(f"{API}/spools/42/weight", json={"weight_grams": 300})

        assert spoolman.view(42)["remaining_weight"] == 100.0
        assert response.json()["weight_used"] == 150.0


@pytest.mark.asyncio
@pytest.mark.integration
class TestKeepOldTare:
    async def test_a_vendor_inherited_tare_is_stamped_on_inheriting_spools(self, async_client: AsyncClient, spoolman):
        spoolman.vendors[3]["empty_spool_weight"] = 216
        spoolman.add_spool(42, 7)
        spoolman.add_spool(43, 7, spool_weight=180)

        response = await async_client.patch(
            f"{API}/filaments/7", json={"spool_weight": 300, "keep_existing_spools": True}
        )

        assert response.status_code == 200
        assert spoolman.spools[42]["spool_weight"] == 216
        assert spoolman.spools[43]["spool_weight"] == 180


@pytest.mark.asyncio
@pytest.mark.integration
class TestWeightSyncs:
    async def test_the_manual_ams_sync_converts_percent_at_the_spools_own_size(
        self, async_client: AsyncClient, spoolman, db_session, printer_factory
    ):
        from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

        printer = await printer_factory()
        spoolman.add_spool(42, 7, initial_weight=250)
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=42))
        await db_session.commit()
        state = SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [{"id": 1, "remain": 40, "remain_g": -1}]}]})

        with patch("backend.app.api.routes.spoolman_inventory.printer_manager.get_status", return_value=state):
            response = await async_client.post(f"{API}/sync-ams-weights")

        assert response.json() == {"synced": 1, "skipped": 0}
        assert spoolman.view(42)["remaining_weight"] == 100.0

    async def test_the_linked_spools_map_reports_the_spools_own_size(self, async_client: AsyncClient, spoolman):
        spoolman.registered.add("tag")
        spoolman.add_spool(42, 7, initial_weight=250, extra={"tag": '"AABBCCDD"'})

        response = await async_client.get("/api/v1/spoolman/spools/linked")

        assert response.status_code == 200
        assert response.json()["linked"]["AABBCCDD"]["filament_weight"] == 250.0

    async def test_the_low_filament_weight_falls_back_to_the_filament(self, db_session, printer_factory, spoolman):
        from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
        from backend.app.services.print_scheduler import PrintScheduler

        printer = await printer_factory()
        spoolman.add_spool(42, 7, initial_weight=None)  # an old spool: only its filament has a size
        spoolman.add_spool(43, 7, initial_weight=250)
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spoolman_spool_id=42))
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=43))
        await db_session.commit()
        loaded = [
            {"ams_id": 0, "tray_id": 0, "global_tray_id": 0},
            {"ams_id": 0, "tray_id": 1, "global_tray_id": 1},
        ]
        scheduler = PrintScheduler()
        client = client_for(spoolman)

        with (
            patch.object(scheduler, "_is_spoolman_mode", AsyncMock(return_value=True)),
            patch("backend.app.services.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
        ):
            weights = await scheduler._inventory_label_weights(db_session, printer.id, loaded)

        assert weights == {0: 1000.0, 1: 250.0}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_clearing_the_preset_preserves_the_catalog_filament(async_client: AsyncClient, spoolman):
    spoolman.add_spool(42, 7, used_weight=456.0)
    url = f"{API}/spools/42"
    linked = await async_client.patch(url, json={"slicer_filament": "GFL99", "slicer_filament_name": "My PLA"})
    assert linked.status_code == 200, linked.text
    assert linked.json()["slicer_filament"] == "GFL99"
    assert linked.json()["slicer_filament_name"] == "My PLA"

    cleared = await async_client.patch(
        url, json={"filament_family_id": None, "slicer_filament": None, "slicer_filament_name": None}
    )
    assert cleared.status_code == 200, cleared.text
    reopened = await async_client.get(url)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["slicer_filament"] is None
    assert reopened.json()["slicer_filament_name"] == "PLA Basic"
    saved = spoolman.view(42)
    assert saved["filament"]["id"] == 7
    assert saved["used_weight"] == 456.0
    assert saved["extra"]["bambu_slicer_filament"] == '""'
    assert saved["extra"]["bambu_slicer_filament_name"] == '""'
