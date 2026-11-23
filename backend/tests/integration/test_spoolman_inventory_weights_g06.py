"""The Spoolman inventory routes read and write a spool's own numbers
(upstream #2906, #3194, #3195, and the bulk-reset leg of c2bc41a5e).

Driven through the real ``SpoolmanClient`` against ``WeighingSpoolman``, a
double that does Spoolman's weight arithmetic — so "the spool is still half
full after a counter reset" is observed on the Spoolman side, not inferred
from which client method was called.
"""

import json
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
@pytest.mark.parametrize("code", [None, "GFL99"], ids=["name-only", "with-code"])
async def test_clearing_the_preset_preserves_the_catalog_filament(async_client: AsyncClient, spoolman, code):
    spoolman.add_spool(42, 7, used_weight=456.0)
    url = f"{API}/spools/42"
    payload = {"slicer_filament_name": "My PLA"}
    if code is not None:
        payload["slicer_filament"] = code
    linked = await async_client.patch(url, json=payload)
    assert linked.status_code == 200, linked.text
    assert linked.json()["slicer_filament"] == code
    assert linked.json()["slicer_filament_name"] == "My PLA"
    assert linked.json()["stored_slicer_filament_name"] == "My PLA"

    listed = await async_client.get(f"{API}/spools")
    assert listed.status_code == 200, listed.text
    [spool] = listed.json()
    assert spool["slicer_filament"] == code
    assert spool["stored_slicer_filament_name"] == "My PLA"

    cleared = await async_client.patch(
        url, json={"filament_family_id": None, "slicer_filament": None, "slicer_filament_name": None}
    )
    assert cleared.status_code == 200, cleared.text
    reopened = await async_client.get(url)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["slicer_filament"] is None
    assert reopened.json()["slicer_filament_name"] == "PLA Basic"
    assert reopened.json()["stored_slicer_filament_name"] is None
    saved = spoolman.view(42)
    assert saved["filament"]["id"] == 7
    assert saved["used_weight"] == 456.0
    assert saved["extra"]["bambu_slicer_filament"] == '""'
    assert saved["extra"]["bambu_slicer_filament_name"] == '""'


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("quantity", [1, 2])
@pytest.mark.parametrize(
    ("code", "name", "clear"),
    [
        (None, None, False),
        (None, "PLA Basic", False),
        ("GFL99", "Legacy PLA", False),
        ("GFL99", None, False),
        ("GFL99", "Legacy PLA", True),
    ],
    ids=["catalog-only", "name-only", "code-and-name", "code-only", "explicit-clear"],
)
async def test_copying_a_spool_retains_the_saved_preset(
    async_client: AsyncClient, spoolman, quantity, code, name, clear
):
    spoolman.add_spool(42, 7, used_weight=456)
    url = f"{API}/spools/42"
    if code is not None or name is not None:
        saved = await async_client.patch(url, json={"slicer_filament": code, "slicer_filament_name": name})
        assert saved.status_code == 200, saved.text
    source = await async_client.get(url)
    assert source.status_code == 200, source.text
    assert source.json()["slicer_filament"] == code
    assert source.json()["stored_slicer_filament_name"] == name

    expected_code, expected_name = (None, None) if clear else (code, name)
    fields = {"spoolman_filament_id": 7, "slicer_filament": expected_code, "slicer_filament_name": expected_name}
    if quantity == 1:
        response = await async_client.post(f"{API}/spools", json=fields)
    else:
        response = await async_client.post(f"{API}/spools/bulk", json={"spool": fields, "quantity": quantity})
    assert response.status_code == 200, response.text
    copies = [response.json()] if quantity == 1 else response.json()
    assert len(copies) == quantity
    assert len({row["id"] for row in copies}) == quantity
    expected_extra = {
        key: json.dumps(value)
        for key, value in (("bambu_slicer_filament", expected_code), ("bambu_slicer_filament_name", expected_name))
        if value is not None
    }
    for copy in copies:
        assert copy["slicer_filament"] == expected_code
        assert copy["stored_slicer_filament_name"] == expected_name
        reopened = await async_client.get(f"{API}/spools/{copy['id']}")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["slicer_filament"] == expected_code
        assert reopened.json()["stored_slicer_filament_name"] == expected_name
        assert reopened.json()["slicer_filament_name"] == (expected_name or "PLA Basic")
        raw = spoolman.view(copy["id"])
        assert raw["extra"] == expected_extra
        assert raw["filament"]["id"] == 7
        assert raw["used_weight"] == 0
    assert spoolman.view(42)["used_weight"] == 456


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("quantity", [1, 2])
async def test_copy_with_an_unregistered_preset_field_does_not_create_spools(
    async_client: AsyncClient, spoolman, quantity
):
    spoolman.field_status = 503
    fields = {"spoolman_filament_id": 7, "slicer_filament_name": "My PLA"}
    if quantity == 1:
        response = await async_client.post(f"{API}/spools", json=fields)
    else:
        response = await async_client.post(f"{API}/spools/bulk", json={"spool": fields, "quantity": quantity})
    assert response.status_code == (502 if quantity == 1 else 500), response.text
    assert not spoolman.spools


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "subtype", "color"),
    [
        ("Matte Scarlet Red", "Matte", "Scarlet Red"),
        ("Silk+ Baby Blue", "Silk+", "Baby Blue"),
        ("Gold - Basic PLA", "Basic", "Gold"),
        ("PLA Dual Color Red/Blue", "Dual Color", "Red/Blue"),
        ("PLA Aero White", "Aero", "White"),
        ("PLA Lite Jade White", "Lite", "Jade White"),
        ("PLA High Speed Black", "High Speed", "Black"),
        ("PLA Dual   Color Red/Blue", "Dual Color", "Red/Blue"),
        ("PLA Engineering", "Engineering", "Engineering"),
    ],
)
async def test_product_name_exposes_finish_and_color(async_client: AsyncClient, spoolman, name, subtype, color):
    spoolman.filaments[7]["name"] = name
    spoolman.add_spool(42, 7)
    response = await async_client.get(f"{API}/spools/42")
    assert response.status_code == 200, response.text
    row = response.json()
    assert (row["subtype"], row["color_name"]) == (subtype, color)
    assert row["color_name_is_synthesized"] is (name == "PLA Engineering")
    assert row["slicer_filament_name"] == name


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("shared", [False, True])
async def test_edit_preserves_spoolman_product_name(async_client: AsyncClient, spoolman, shared):
    product_name = "Matte Scarlet Red"
    spoolman.filaments[7]["name"] = product_name
    spoolman.add_spool(42, 7, used_weight=50)
    if shared:
        spoolman.add_spool(43, 7)

    url = f"{API}/spools/42"
    saved = await async_client.patch(url, json={"subtype": "Matte", "note": "Shelf A"})
    assert saved.status_code == 200, saved.text
    assert spoolman.view(42)["filament"]["id"] == 7
    assert spoolman.filaments[7]["name"] == product_name
    assert not spoolman.bodies("PATCH", "/filament/7")
    assert not spoolman.bodies("POST", "/filament")

    changed = await async_client.patch(url, json={"rgba": "110000FF", "color_name": "Custom Red"})
    assert changed.status_code == 200, changed.text
    raw = spoolman.view(42)
    assert raw["filament"]["name"] == product_name
    assert raw["filament"]["color_hex"] == "110000"
    assert raw["used_weight"] == 50
    assert (raw["filament"]["id"] != 7) is shared
    if shared:
        assert spoolman.view(43)["filament"]["color_hex"] == "FF0000"
    reopened = await async_client.get(url)
    assert reopened.json()["color_name"] == "Custom Red"

    subtype_edit = await async_client.patch(url, json={"subtype": "Basic"})
    assert subtype_edit.status_code == 200, subtype_edit.text
    assert spoolman.view(42)["filament"]["name"] == "PLA Basic"
    if shared:
        assert spoolman.view(43)["filament"]["name"] == product_name
