"""Regression tests for D.2: SpoolmanDB external-library lookup filtered by
manufacturer == "Bambu Lab".

Upstream Bambuddy #1330 / commit a1d6fb22. The /api/v1/external/filament
endpoint returns the full multi-vendor SpoolmanDB catalogue with no
server-side filter — without a manufacturer check, Bambu Lab RFID spools
get auto-labelled with competitor product names because the catalogue is
ID-sorted and competitor entries come first.

These tests pin:
- Manufacturer filter rejects non-Bambu entries.
- ``id.startswith("bambulab_")`` is the defensive fallback for schema drift.
- When multiple Bambu Lab candidates match material+color, the one whose
  ``name`` equals ``tray_sub_brands`` wins (specific over generic).
- Falls through to ``create_filament`` when no Bambu match exists.
- Density from the chosen external entry forwards into ``create_filament``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.app.services.spoolman import AMSTray, SpoolmanClient


def _tray(*, material: str = "PLA", color: str = "000000FF", sub_brand: str = "PLA Basic") -> AMSTray:
    return AMSTray(
        ams_id=0,
        tray_id=0,
        tray_type=material,
        tray_sub_brands=sub_brand,
        tray_color=color,
        remain=100,
        tag_uid="aabbccdd",
        tray_uuid="11111111-2222-3333-4444-555555555555",
        tray_info_idx="GFA00",
        tray_weight=1000,
    )


def _client_with_externals(externals: list[dict]) -> SpoolmanClient:
    client = SpoolmanClient(base_url="http://spoolman.example")
    client.ensure_bambu_vendor = AsyncMock(return_value=1)
    client.get_filaments = AsyncMock(return_value=[])  # no internal match
    client.get_external_filaments = AsyncMock(return_value=externals)
    client._create_filament_from_external = AsyncMock(side_effect=lambda ext, tray: {"id": 999, **ext})
    client.create_filament = AsyncMock(return_value={"id": 998, "name": "fresh"})
    return client


class TestManufacturerFilter:
    @pytest.mark.asyncio
    async def test_skips_non_bambu_entries(self):
        """A 3DJAKE PLA Black entry must NOT be picked even though material+color match."""
        client = _client_with_externals(
            [
                {
                    "id": "3djake_pla_black",
                    "manufacturer": "3DJAKE",
                    "material": "PLA",
                    "color_hex": "000000",
                    "name": "PLA Black",
                },
            ]
        )
        await client._find_or_create_filament(_tray(color="000000FF"))
        # No external match → fell through to create_filament with the
        # Bambu Lab vendor (not from the 3DJAKE entry).
        client._create_filament_from_external.assert_not_called()
        client.create_filament.assert_called_once()

    @pytest.mark.asyncio
    async def test_picks_bambu_lab_entry(self):
        client = _client_with_externals(
            [
                {"id": "3djake_pla_black", "manufacturer": "3DJAKE", "material": "PLA", "color_hex": "000000"},
                {
                    "id": "bambulab_pla_black",
                    "manufacturer": "Bambu Lab",
                    "material": "PLA",
                    "color_hex": "000000",
                    "name": "PLA Basic",
                },
            ]
        )
        await client._find_or_create_filament(_tray(color="000000FF"))
        client._create_filament_from_external.assert_called_once()
        chosen = client._create_filament_from_external.call_args.args[0]
        assert chosen["manufacturer"] == "Bambu Lab"

    @pytest.mark.asyncio
    async def test_id_prefix_fallback_when_manufacturer_missing(self):
        """Defensive: an entry missing ``manufacturer`` still wins if its
        id starts with ``bambulab_`` — schema drift fallback."""
        client = _client_with_externals(
            [
                {"id": "bambulab_pla_black", "material": "PLA", "color_hex": "000000", "name": "PLA Basic"},
            ]
        )
        await client._find_or_create_filament(_tray(color="000000FF"))
        client._create_filament_from_external.assert_called_once()


class TestSubBrandTiebreaker:
    @pytest.mark.asyncio
    async def test_specific_sub_brand_wins_over_generic(self):
        """When multiple Bambu candidates match material+color, the one
        whose ``name`` equals ``tray_sub_brands`` (e.g. "PLA Basic") wins
        over a generic "Black" entry."""
        client = _client_with_externals(
            [
                {
                    "id": "bambulab_pla_black_generic",
                    "manufacturer": "Bambu Lab",
                    "material": "PLA",
                    "color_hex": "000000",
                    "name": "Black",
                },
                {
                    "id": "bambulab_pla_basic_black",
                    "manufacturer": "Bambu Lab",
                    "material": "PLA",
                    "color_hex": "000000",
                    "name": "PLA Basic",
                },
            ]
        )
        await client._find_or_create_filament(_tray(color="000000FF", sub_brand="PLA Basic"))
        chosen = client._create_filament_from_external.call_args.args[0]
        assert chosen["name"] == "PLA Basic", "specific sub_brand match must win over generic"

    @pytest.mark.asyncio
    async def test_no_sub_brand_match_falls_back_to_first(self):
        """If no entry's ``name`` matches the tray's sub_brand, the first
        Bambu candidate is used as a deterministic fallback."""
        client = _client_with_externals(
            [
                {
                    "id": "bambulab_a",
                    "manufacturer": "Bambu Lab",
                    "material": "PLA",
                    "color_hex": "000000",
                    "name": "Black",
                },
                {
                    "id": "bambulab_b",
                    "manufacturer": "Bambu Lab",
                    "material": "PLA",
                    "color_hex": "000000",
                    "name": "Matte Black",
                },
            ]
        )
        await client._find_or_create_filament(_tray(color="000000FF", sub_brand="PLA Carbon"))
        chosen = client._create_filament_from_external.call_args.args[0]
        assert chosen["id"] == "bambulab_a", "deterministic fallback to first Bambu candidate"


class TestDensityForwarding:
    @pytest.mark.asyncio
    async def test_density_from_external_passed_to_create_filament(self):
        """``_create_filament_from_external`` forwards ``density`` so the
        PLA-default 1.24 doesn't silently overwrite the catalogue's value."""
        client = SpoolmanClient(base_url="http://spoolman.example")
        client.ensure_bambu_vendor = AsyncMock(return_value=1)
        client.create_filament = AsyncMock(return_value={"id": 999})

        external = {
            "id": "bambulab_pla_basic_black",
            "manufacturer": "Bambu Lab",
            "material": "PLA",
            "color_hex": "000000",
            "name": "PLA Basic",
            "density": 1.31,  # Bambu's catalogue value, distinct from PLA default
            "weight": 1000,
        }
        tray = _tray(color="000000FF")
        await client._create_filament_from_external(external, tray)

        kwargs = client.create_filament.call_args.kwargs
        assert kwargs.get("density") == 1.31, "density from external entry must forward"
