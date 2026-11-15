"""The operator's spool order (Settings → Filament): a chain of fields with directions that the slot-assignment
picker follows, and the Filaments list too whenever no column is chosen. Empty = each list's old order."""

import pytest

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool

PICKER = "/api/v1/inventory/spools/picker?printer_id=1&ams_id=0&tray_id=0&show_all=true"
LIST = "/api/v1/inventory/spools?archived=active&page=1&per_page=50"


async def _stock(db_session):
    """Four spools whose default order (material, brand, colour) differs from every order asked below."""
    rows = {
        "petg_full": Spool(material="PETG", brand="Beta", color_name="Red", label_weight=1000, core_weight=250),
        "pla_half": Spool(
            material="PLA", brand="Alpha", color_name="Blue", label_weight=1000, core_weight=250, weight_used=500
        ),
        "pla_low": Spool(
            material="PLA", brand="Alpha", color_name="Azure", label_weight=1000, core_weight=250, weight_used=900
        ),
        "abs_full": Spool(material="ABS", brand="Gamma", color_name="White", label_weight=1000, core_weight=250),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    return {name: spool.id for name, spool in rows.items()}


def _ids(response):
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"]]


class TestParsing:
    def test_unknown_fields_bad_directions_and_repeats_are_dropped(self):
        from backend.app.services.inventory_service import parse_spool_sort_order

        assert parse_spool_sort_order(" material_asc, net_desc ,bogus_asc,brand_up,material_desc,,") == [
            ("material", "asc"),
            ("net", "desc"),
        ]
        assert parse_spool_sort_order("") == []
        assert parse_spool_sort_order(None) == []


@pytest.mark.asyncio
@pytest.mark.integration
class TestPickerFollowsTheOrder:
    async def test_empty_setting_keeps_material_brand_colour(self, async_client, db_session):
        ids = await _stock(db_session)
        assert _ids(await async_client.get(PICKER)) == [
            ids["abs_full"],
            ids["petg_full"],
            ids["pla_low"],
            ids["pla_half"],
        ]

    async def test_a_chain_orders_level_by_level(self, async_client, db_session):
        ids = await _stock(db_session)
        db_session.add(Settings(key="spool_sort_order", value="material_desc,net_asc"))
        await db_session.commit()
        assert _ids(await async_client.get(PICKER)) == [
            ids["pla_low"],
            ids["pla_half"],
            ids["petg_full"],
            ids["abs_full"],
        ]

    async def test_the_composed_name_is_a_field(self, async_client, db_session):
        """«Name» sorts by what the naming template composes — here colour first, so Azure < Blue < Red < White."""
        ids = await _stock(db_session)
        db_session.add(Settings(key="spool_display_template", value="{color_name} {brand}"))
        db_session.add(Settings(key="spool_sort_order", value="display_name_asc"))
        await db_session.commit()
        assert _ids(await async_client.get(PICKER)) == [
            ids["pla_low"],
            ids["pla_half"],
            ids["petg_full"],
            ids["abs_full"],
        ]

    async def test_equal_rows_keep_one_order_across_pages(self, async_client, db_session):
        db_session.add_all(
            [
                Spool(material="PLA", brand="Same", color_name="Same", label_weight=1000, core_weight=250)
                for _ in range(7)
            ]
        )
        db_session.add(Settings(key="spool_sort_order", value="material_asc"))
        await db_session.commit()
        first = _ids(await async_client.get(f"{PICKER}&per_page=4&page=1"))
        second = _ids(await async_client.get(f"{PICKER}&per_page=4&page=2"))
        assert len(set(first + second)) == 7
        assert first + second == sorted(first + second, reverse=True), "the id breaks ties, newest first"


@pytest.mark.asyncio
@pytest.mark.integration
class TestFilamentsListFollowsTheOrder:
    async def test_no_column_chosen_uses_the_chain(self, async_client, db_session):
        ids = await _stock(db_session)
        db_session.add(Settings(key="spool_sort_order", value="net_asc,id_asc"))
        await db_session.commit()
        assert _ids(await async_client.get(LIST)) == [
            ids["pla_low"],
            ids["pla_half"],
            ids["petg_full"],
            ids["abs_full"],
        ]

    async def test_a_chosen_column_wins(self, async_client, db_session):
        ids = await _stock(db_session)
        db_session.add(Settings(key="spool_sort_order", value="net_asc"))
        await db_session.commit()
        assert _ids(await async_client.get(f"{LIST}&sort_by=id_asc")) == sorted(ids.values())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_order_reaches_readers_without_settings_rights(async_client, db_session):
    db_session.add(Settings(key="spool_sort_order", value="brand_desc"))
    await db_session.commit()
    response = await async_client.get("/api/v1/settings/ui-preferences")
    assert response.status_code == 200, response.text
    assert response.json()["spool_sort_order"] == "brand_desc"
