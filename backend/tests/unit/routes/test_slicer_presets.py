"""Tests for the unified slicer-presets dedup logic + URL resolver.

Pure module-level tests; live HTTP / DB paths are covered by the integration
tests in Phase 1.E once the slice routes themselves land.
"""

from __future__ import annotations

import pytest

from backend.app.api.routes.slicer_presets import (
    _dedupe_by_name,
    _empty_slots,
    _parse_filament_metadata,
)
from backend.app.schemas.slicer_presets import UnifiedPreset


def _slots(**overrides) -> dict:
    base = _empty_slots()
    base.update(overrides)
    return base


class TestDedupePriority:
    def test_cloud_wins_over_local_and_standard(self):
        cloud = _slots(printer=[UnifiedPreset(id="c1", name="A1 0.4 nozzle", source="cloud")])
        local = _slots(printer=[UnifiedPreset(id="42", name="A1 0.4 nozzle", source="local")])
        standard = _slots(printer=[UnifiedPreset(id="A1 0.4 nozzle", name="A1 0.4 nozzle", source="standard")])
        out_cloud, out_local, out_standard = _dedupe_by_name(cloud, local, standard)
        assert [p.name for p in out_cloud["printer"]] == ["A1 0.4 nozzle"]
        assert out_local["printer"] == []
        assert out_standard["printer"] == []

    def test_local_wins_over_standard(self):
        cloud = _slots()
        local = _slots(process=[UnifiedPreset(id="7", name="0.20mm Standard", source="local")])
        standard = _slots(process=[UnifiedPreset(id="0.20mm Standard", name="0.20mm Standard", source="standard")])
        out_cloud, out_local, out_standard = _dedupe_by_name(cloud, local, standard)
        assert out_local["process"][0].source == "local"
        assert out_standard["process"] == []

    def test_disjoint_names_all_present(self):
        cloud = _slots(filament=[UnifiedPreset(id="c1", name="My PLA", source="cloud")])
        local = _slots(filament=[UnifiedPreset(id="3", name="Imported PETG", source="local")])
        standard = _slots(filament=[UnifiedPreset(id="Bambu PLA Basic", name="Bambu PLA Basic", source="standard")])
        out_cloud, out_local, out_standard = _dedupe_by_name(cloud, local, standard)
        assert len(out_cloud["filament"]) == 1
        assert len(out_local["filament"]) == 1
        assert len(out_standard["filament"]) == 1


class TestFilamentMetadataMerge:
    def test_cloud_inherits_local_filament_metadata_on_dedup(self):
        """When a cloud entry wins over a same-named local entry, the cloud
        entry inherits filament_type + filament_colour from the local row.
        Cloud doesn't carry metadata (rate-limited detail endpoint), so without
        this merge the SliceModal's pre-pick loses match info for every preset
        the user has cloud-synced AND locally imported."""
        cloud = _slots(filament=[UnifiedPreset(id="c", name="Bambu PLA Basic", source="cloud")])
        local = _slots(
            filament=[
                UnifiedPreset(
                    id="9",
                    name="Bambu PLA Basic",
                    source="local",
                    filament_type="PLA",
                    filament_colour="#00FF00",
                )
            ]
        )
        standard = _slots()
        out_cloud, _ol, _os = _dedupe_by_name(cloud, local, standard)
        assert out_cloud["filament"][0].filament_type == "PLA"
        assert out_cloud["filament"][0].filament_colour == "#00FF00"

    def test_cloud_keeps_own_metadata_when_present(self):
        cloud = _slots(
            filament=[
                UnifiedPreset(
                    id="c",
                    name="My Custom",
                    source="cloud",
                    filament_type="PETG",
                    filament_colour="#FF0000",
                )
            ]
        )
        local = _slots(
            filament=[
                UnifiedPreset(
                    id="9",
                    name="My Custom",
                    source="local",
                    filament_type="PLA",  # would conflict if we naively overwrote
                    filament_colour="#00FF00",
                )
            ]
        )
        out_cloud, _ol, _os = _dedupe_by_name(cloud, local, _empty_slots())
        # Cloud's own non-None metadata MUST win — that's the user's actual
        # cloud preset content, even if it happens to share a name with a
        # local import.
        assert out_cloud["filament"][0].filament_type == "PETG"
        assert out_cloud["filament"][0].filament_colour == "#FF0000"


class TestFilamentMetadataParse:
    def test_array_first_value_extracted(self):
        out = _parse_filament_metadata('{"filament_type":["PLA","-"],"filament_colour":["#FF8800"]}')
        assert out == ("PLA", "#FF8800")

    def test_string_value_returned(self):
        out = _parse_filament_metadata('{"filament_type":"PLA"}')
        assert out == ("PLA", None)

    def test_corrupt_json_returns_none(self):
        out = _parse_filament_metadata("not json {{")
        assert out == (None, None)

    def test_non_dict_returns_none(self):
        out = _parse_filament_metadata("[1,2,3]")
        assert out == (None, None)

    def test_empty_returns_none(self):
        out = _parse_filament_metadata("")
        assert out == (None, None)

    def test_none_returns_none(self):
        out = _parse_filament_metadata(None)
        assert out == (None, None)
