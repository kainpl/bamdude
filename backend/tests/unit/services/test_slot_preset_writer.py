"""Unit tests for slot_preset_writer — the shared upsert that keeps the AMS
slot card's displayed preset name in sync after a spool swap (E8).

The two convenience wrappers derive the (preset_id, preset_name, preset_source)
triple and defer to the ``upsert_slot_preset`` primitive. These tests patch the
primitive to capture the derived triple, so they exercise the derivation logic
without a DB. The primitive's own empty-key no-op is checked directly.
"""

from types import SimpleNamespace

import pytest

from backend.app.services import slot_preset_writer


@pytest.mark.asyncio
class TestUpsertSlotPresetForSpool:
    """Internal-mode wrapper: derive the triple from a Spool ORM object."""

    async def _capture(self, monkeypatch):
        captured = {}

        async def fake_upsert(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(slot_preset_writer, "upsert_slot_preset", fake_upsert)
        return captured

    async def test_numeric_slicer_filament_gives_local_id(self, monkeypatch):
        captured = await self._capture(monkeypatch)
        spool = SimpleNamespace(slicer_filament_name="My PLA", slicer_filament="50")
        await slot_preset_writer.upsert_slot_preset_for_spool(
            db=None, spool=spool, printer_id=1, ams_id=0, tray_id=2, tray_info_idx="GFA00"
        )
        assert captured["preset_id"] == "local_50"
        assert captured["preset_source"] == "local"
        assert captured["preset_name"] == "My PLA"

    async def test_underscore_prefixed_local_id_uses_base(self, monkeypatch):
        captured = await self._capture(monkeypatch)
        spool = SimpleNamespace(slicer_filament_name="PLA", slicer_filament="50_1")
        await slot_preset_writer.upsert_slot_preset_for_spool(
            db=None, spool=spool, printer_id=1, ams_id=0, tray_id=0, tray_info_idx="GFA00"
        )
        assert captured["preset_id"] == "local_50"
        assert captured["preset_source"] == "local"

    async def test_non_numeric_slicer_filament_falls_back_to_tray_idx(self, monkeypatch):
        captured = await self._capture(monkeypatch)
        # A non-numeric slicer_filament → cloud id derived from tray_info_idx.
        spool = SimpleNamespace(slicer_filament_name="Bambu PLA", slicer_filament="GFA00")
        await slot_preset_writer.upsert_slot_preset_for_spool(
            db=None, spool=spool, printer_id=1, ams_id=0, tray_id=0, tray_info_idx="GFA00"
        )
        assert captured["preset_source"] == "cloud"
        assert captured["preset_id"]  # derived from filament_id_to_setting_id, non-empty

    async def test_name_falls_back_to_sub_brands_then_type(self, monkeypatch):
        captured = await self._capture(monkeypatch)
        spool = SimpleNamespace(slicer_filament_name="", slicer_filament="")
        await slot_preset_writer.upsert_slot_preset_for_spool(
            db=None,
            spool=spool,
            printer_id=1,
            ams_id=0,
            tray_id=0,
            tray_sub_brands="Bambu PETG",
            tray_type="PETG",
        )
        assert captured["preset_name"] == "Bambu PETG"


@pytest.mark.asyncio
class TestUpsertSlotPresetForSpoolmanSpool:
    """Spoolman-mode wrapper: derive from the Spoolman spool dict shape."""

    async def _capture(self, monkeypatch):
        captured = {}

        async def fake_upsert(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(slot_preset_writer, "upsert_slot_preset", fake_upsert)
        return captured

    async def test_name_from_filament_name(self, monkeypatch):
        captured = await self._capture(monkeypatch)
        await slot_preset_writer.upsert_slot_preset_for_spoolman_spool(
            db=None,
            spoolman_spool={"filament": {"name": "Polar White PLA"}},
            tray_info_idx="GFA00",
            tray_sub_brands="",
            tray_type="PLA",
            printer_id=1,
            ams_id=0,
            tray_id=0,
        )
        assert captured["preset_name"] == "Polar White PLA"
        assert captured["preset_source"] == "cloud"

    async def test_name_falls_back_to_material_then_type(self, monkeypatch):
        captured = await self._capture(monkeypatch)
        await slot_preset_writer.upsert_slot_preset_for_spoolman_spool(
            db=None,
            spoolman_spool={"filament": {"material": "PETG"}},
            tray_info_idx="",
            tray_sub_brands="",
            tray_type="PETG",
            printer_id=1,
            ams_id=0,
            tray_id=0,
        )
        assert captured["preset_name"] == "PETG"


@pytest.mark.asyncio
class TestUpsertSlotPresetPrimitive:
    async def test_empty_preset_id_is_noop(self):
        # No DB session touched — an empty preset_id short-circuits before any IO.
        sentinel = object()
        await slot_preset_writer.upsert_slot_preset(
            db=sentinel, printer_id=1, ams_id=0, tray_id=0, preset_id="", preset_name="x"
        )
