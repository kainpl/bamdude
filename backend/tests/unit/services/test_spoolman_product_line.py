"""A Bambu roll is matched to its own product line, not just its colour (upstream cb924e5c4, #2907).

Material plus colour is not an identity: PLA Basic Black and PLA Matte Charcoal
are both PLA at ``000000``, so a Matte roll auto-created by the AMS sync was
hung on the Basic filament and read as "Basic" everywhere. The product line is
the tray's ``tray_sub_brands`` (our family catalogue's alias when the tray says
nothing), the colour name comes from the colour catalogue — no table of names
lives in the code.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.color_catalog import ColorCatalogEntry
from backend.app.services.color_catalog_lookup import (
    CatalogColor,
    best_product_line_match,
    product_line_rank,
    resolve_bambu_color,
    tray_product_line,
)
from backend.app.services.spoolman import AMSTray, SpoolmanClient

BAMBU_VENDOR = 2
CHARCOAL = CatalogColor("Charcoal")
BLACK = CatalogColor("Black")


def _tray(*, sub_brand: str = "PLA Matte", color: str = "000000FF", idx: str = "GFA01", material: str = "PLA"):
    return AMSTray(
        ams_id=0,
        tray_id=0,
        tray_type=material,
        tray_sub_brands=sub_brand,
        tray_color=color,
        remain=100,
        tag_uid="",
        tray_uuid="A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
        tray_info_idx=idx,
        tray_weight=1000,
    )


def _filament(fid: int, name: str, *, color: str = "000000", material: str = "PLA") -> dict:
    return {"id": fid, "name": name, "material": material, "color_hex": color, "vendor_id": BAMBU_VENDOR}


def _library(name: str, *, color: str = "000000", material: str = "PLA", **more) -> dict:
    return {
        "id": f"bambulab_{name}",
        "manufacturer": "Bambu Lab",
        "name": name,
        "material": material,
        "color_hex": color,
        **more,
    }


def _client(filaments: list[dict], library: list[dict] | None = None) -> SpoolmanClient:
    client = SpoolmanClient("http://localhost:7912")
    client.ensure_bambu_vendor = AsyncMock(return_value=BAMBU_VENDOR)
    client.get_filaments = AsyncMock(return_value=filaments)
    client.get_external_filaments = AsyncMock(return_value=library or [])
    client.create_filament = AsyncMock(side_effect=lambda **kw: {"id": 900, **kw})
    return client


class TestTheRank:
    def test_the_sub_brand_itself_is_the_best_name(self):
        assert product_line_rank("PLA Matte", "Charcoal", "PLA Matte", "PLA") == 0

    @pytest.mark.parametrize("name", ["Matte Charcoal", "Charcoal Matte", "matte  charcoal"])
    def test_the_line_folded_into_the_colour_is_next(self, name):
        assert product_line_rank(name, "Charcoal", "PLA Matte", "PLA") == 1

    def test_the_librarys_plus_is_part_of_the_product_name_not_the_line(self):
        assert product_line_rank("Tough+ Black", "Black", "PLA Tough", "PLA") == 1

    def test_the_bare_colour_is_the_weakest(self):
        assert product_line_rank("Black", "Black", "PLA Basic", "PLA") == 2

    def test_another_lines_name_does_not_identify_the_roll(self):
        assert product_line_rank("PLA Basic", "Charcoal", "PLA Matte", "PLA") is None
        assert product_line_rank("Black", "Charcoal", "PLA Matte", "PLA") is None

    def test_without_a_colour_name_only_the_sub_brand_counts(self):
        assert product_line_rank("PLA Matte", None, "PLA Matte", "PLA") == 0
        assert product_line_rank("Matte Charcoal", None, "PLA Matte", "PLA") is None

    def test_the_most_specific_wins_and_the_first_wins_a_tie(self):
        bare, folded, exact = _filament(1, "Charcoal"), _filament(2, "Matte Charcoal"), _filament(3, "PLA Matte")
        assert best_product_line_match([bare, folded, exact], "Charcoal", "PLA Matte", "PLA") is exact
        assert best_product_line_match([bare, folded], "Charcoal", "PLA Matte", "PLA") is folded
        twin = _filament(4, "Charcoal")
        assert best_product_line_match([bare, twin], "Charcoal", "PLA Matte", "PLA") is bare
        assert best_product_line_match([_filament(5, "PLA Basic")], "Charcoal", "PLA Matte", "PLA") is None


class TestTheProductLine:
    def test_it_is_what_the_tray_reports(self):
        assert tray_product_line("PLA Matte", "GFA01") == "PLA Matte"

    def test_a_tray_that_says_nothing_takes_it_from_the_family_catalogue(self):
        """``GFA01`` is Bambu PLA Matte in our catalogue; the vendor is not part of the line."""
        assert tray_product_line("", "GFA01") == "PLA Matte"

    def test_an_unknown_preset_has_no_line(self):
        assert tray_product_line("", "no-such") == ""


@pytest.mark.asyncio
class TestTheCatalogueColour:
    async def _seed(self, db_session):
        db_session.add_all(
            [
                ColorCatalogEntry(
                    manufacturer="Bambu Lab", color_name="Jade White", hex_color="#FFFFFF", material="PLA Basic"
                ),
                ColorCatalogEntry(
                    manufacturer="Bambu Lab", color_name="Ivory White", hex_color="#FFFFFF", material="PLA Matte"
                ),
                ColorCatalogEntry(manufacturer="Other", color_name="Snow", hex_color="#FFFFFF", material="PLA Matte"),
            ]
        )
        await db_session.commit()

    async def test_the_same_hex_is_named_by_its_product_line(self, db_session):
        await self._seed(db_session)
        assert (await resolve_bambu_color(db_session, "FFFFFFFF", "PLA Matte")).name == "Ivory White"
        assert (await resolve_bambu_color(db_session, "FFFFFFFF", "PLA Basic")).name == "Jade White"

    async def test_a_colour_the_catalogue_does_not_know_has_no_name(self, db_session):
        await self._seed(db_session)
        assert await resolve_bambu_color(db_session, "123456FF", "PLA Matte") is None
        assert await resolve_bambu_color(db_session, "FFFFFFFF", "PLA Silk") is None

    async def test_a_clear_roll_is_clear_without_asking_the_catalogue(self, db_session):
        assert (await resolve_bambu_color(db_session, "00000000", "PLA Basic")).name == "Clear"


@pytest.mark.asyncio
class TestAnExistingFilamentIsPickedByProductLine:
    async def test_a_matte_roll_does_not_hang_on_the_basic_filament_of_the_same_hex(self):
        client = _client([_filament(1, "PLA Basic")])

        result = await client._find_or_create_filament(_tray(), CHARCOAL)

        assert result["id"] == 900
        assert client.create_filament.call_args.kwargs["name"] == "PLA Matte"

    async def test_its_own_lines_filament_is_reused_even_when_another_comes_first(self):
        basic, matte = _filament(1, "PLA Basic"), _filament(2, "PLA Matte")
        client = _client([basic, matte])

        assert await client._find_or_create_filament(_tray(), CHARCOAL) is matte
        client.create_filament.assert_not_called()

    async def test_an_instance_holding_only_a_bare_colour_filament_reuses_it(self):
        """No duplicate for every spool on an existing instance."""
        bare = _filament(1, "Black")
        client = _client([bare])

        assert await client._find_or_create_filament(_tray(sub_brand="PLA Basic", idx="GFA00"), BLACK) is bare

    async def test_a_colour_the_catalogue_does_not_know_is_built_from_the_tray(self):
        client = _client([_filament(1, "Black")], [_library("Black")])

        result = await client._find_or_create_filament(_tray(), None)

        assert result["id"] == 900
        assert client.create_filament.call_args.kwargs["name"] == "PLA Matte"

    async def test_a_caller_that_resolved_no_colour_at_all_keeps_the_first_match(self):
        """Without a session (no catalogue to ask) nothing changes: the first
        same-colour Bambu filament, as before."""
        first = _filament(1, "Black")
        client = _client([first, _filament(2, "PLA Matte")])

        assert await client._find_or_create_filament(_tray()) is first


@pytest.mark.asyncio
class TestALibraryEntryIsPickedByProductLine:
    async def test_the_entry_of_the_rolls_line_wins_and_the_filament_is_named_by_the_tray(self):
        client = _client([], [_library("Black", density=1.24), _library("Matte Charcoal", density=1.31, weight=1000)])

        await client._find_or_create_filament(_tray(), CHARCOAL)

        created = client.create_filament.call_args.kwargs
        assert created["name"] == "PLA Matte"  # not the library's "Matte Charcoal"
        assert created["density"] == 1.31

    async def test_no_entry_of_that_line_means_built_from_the_tray_not_the_first_candidate(self):
        client = _client([], [_library("Black", density=9.99)])

        await client._find_or_create_filament(_tray(), CHARCOAL)

        created = client.create_filament.call_args.kwargs
        assert created["name"] == "PLA Matte"
        assert created.get("density") != 9.99


@pytest.mark.asyncio
class TestAutoCreateStoresTheColourName:
    async def _sync(self, db_session, tray):
        client = SpoolmanClient("http://localhost:7912")
        with (
            patch.object(client, "find_spool_by_tag", AsyncMock(return_value=None)),
            patch.object(client, "_find_or_create_filament", AsyncMock(return_value={"id": 1})) as find,
            patch.object(client, "create_spool", AsyncMock(return_value={"id": 99})) as create,
        ):
            await client.sync_ams_tray(tray, "X1C", disable_weight_sync=True, db=db_session)
        return find, create.call_args.kwargs["extra"]

    async def test_the_catalogue_name_goes_on_the_spool(self, db_session):
        db_session.add(
            ColorCatalogEntry(
                manufacturer="Bambu Lab", color_name="Charcoal", hex_color="#000000", material="PLA Matte"
            )
        )
        await db_session.commit()

        find, extra = await self._sync(db_session, _tray())

        assert extra["bambu_color_name"] == json.dumps("Charcoal")
        assert find.call_args.args[1] == CatalogColor("Charcoal")

    async def test_a_clear_roll_is_named_clear(self, db_session):
        _, extra = await self._sync(db_session, _tray(color="00000000"))

        assert extra["bambu_color_name"] == json.dumps("Clear")

    async def test_a_colour_the_catalogue_does_not_know_stores_no_name(self, db_session):
        find, extra = await self._sync(db_session, _tray(color="123456FF"))

        assert "bambu_color_name" not in extra
        assert find.call_args.args[1] is None
