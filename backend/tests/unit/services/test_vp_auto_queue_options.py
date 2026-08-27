"""Slicer print options reach the AUTO-QUEUE tier of the virtual printer.

The vault-tracked hole: ``on_print_command`` early-returned for every mode
but ``print_queue``, so a VP distributing работу across the farm never even
CAPTURED the slicer's options — the router row was built from column defaults
and the distributor faithfully copied those defaults to the real printer.
Fixed 2026-08-28: the stash serves both queue-building modes, the auto row
carries the six flags + the nozzle pick (m156), and the late-MQTT retro stamp
patches still-pending auto rows the wait window missed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.virtual_printer.manager import VirtualPrinterInstance


def _instance(mode="auto_queue"):
    vp = VirtualPrinterInstance.__new__(VirtualPrinterInstance)
    vp.name = "vp-test"
    vp.mode = mode
    vp._mqtt = None
    vp._slicer_print_options = {}
    vp._slicer_print_options_events = {}
    vp._recent_queue_items = {}
    vp._recent_auto_items = {}
    vp._session_factory = None
    return vp


class TestStashServesBothModes:
    @pytest.mark.asyncio
    async def test_take_pops_the_stashed_options(self):
        vp = _instance()
        vp._slicer_print_options["a.3mf"] = {"timelapse": True}
        assert await vp._take_slicer_options("a.3mf") == {"timelapse": True}
        assert vp._slicer_print_options == {}

    @pytest.mark.asyncio
    async def test_no_mqtt_means_no_wait(self):
        vp = _instance()
        assert await vp._take_slicer_options("missing.3mf") is None


class TestNozzleMappingParse:
    def test_list_becomes_json(self):
        assert VirtualPrinterInstance._parse_nozzle_mapping({"nozzle_mapping": [1, 0]}) == json.dumps([1, 0])

    def test_json_string_accepted(self):
        assert VirtualPrinterInstance._parse_nozzle_mapping({"nozzle_mapping": "[1, 0]"}) == json.dumps([1, 0])

    def test_bad_json_fails_open(self):
        assert VirtualPrinterInstance._parse_nozzle_mapping({"nozzle_mapping": "not json"}) is None

    def test_absent_is_none(self):
        assert VirtualPrinterInstance._parse_nozzle_mapping(None) is None
        assert VirtualPrinterInstance._parse_nozzle_mapping({}) is None


class TestRetroStampAutoItem:
    @pytest.mark.asyncio
    async def test_patches_pending_rows_inside_the_ttl(self):
        import time as _time

        vp = _instance()
        row = SimpleNamespace(
            id=7, status="pending", bed_levelling=True, flow_cali=True, timelapse=False, nozzle_mapping=None
        )
        result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        db.__aenter__ = AsyncMock(return_value=db)
        db.__aexit__ = AsyncMock(return_value=False)
        vp._session_factory = lambda: db
        vp._recent_auto_items["a.3mf"] = ([7], _time.monotonic())

        await vp._restamp_recent_auto_item(
            "a.3mf", {"timelapse": True, "bed_leveling": False, "nozzle_mapping": [1, 0]}
        )

        assert row.timelapse is True
        assert row.bed_levelling is False
        assert row.nozzle_mapping == json.dumps([1, 0])
        db.commit.assert_awaited_once()
        assert "a.3mf" not in vp._recent_auto_items

    @pytest.mark.asyncio
    async def test_expired_entry_is_dropped_untouched(self):
        import time as _time

        vp = _instance()
        db = AsyncMock()
        vp._session_factory = lambda: db
        vp._recent_auto_items["a.3mf"] = ([7], _time.monotonic() - 3600)

        await vp._restamp_recent_auto_item("a.3mf", {"timelapse": True})

        db.execute.assert_not_awaited()
        assert "a.3mf" not in vp._recent_auto_items
