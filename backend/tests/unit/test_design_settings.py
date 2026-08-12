"""Carrying a designer's own process tweaks across a re-slice (#2622).

A published model often deviates from the stock preset on purpose — five walls,
100 % infill, a 0.1 mm first layer. Re-slicing it for another printer dropped all
of it, because ``--load-settings`` is authoritative and the picked process preset
wins over the file's embedded config.

The deviation list does not have to be computed: BambuStudio writes it into the
3MF as ``different_settings_to_system``, laid out ``[process, *filaments,
printer]``. Two things carry the risk, and both are pinned here — **reading the
right slot**, because taking the printer slot for the process slot would carry
the designer's ``machine_start_gcode`` onto a foreign machine, and **which keys
are safe**, because a speed tuned for their kinematics can be merely wrong on the
target or outside the range its profile accepts, which fails the slice outright.
"""

from __future__ import annotations

import io
import json
import zipfile

from backend.app.services.design_settings import (
    apply_design_overrides,
    extract_design_process_overrides,
    is_printer_coupled,
    overrides_from_config,
)


def _config(changed: list[str], filaments: int = 1, **values):
    return {
        "different_settings_to_system": changed,
        "filament_settings_id": [f"F{i}" for i in range(filaments)],
        **values,
    }


def _three_mf(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(config))
    return buf.getvalue()


class TestReadingTheRightSlot:
    def test_the_process_slot_is_index_zero(self) -> None:
        cfg = _config(
            ["wall_loops;sparse_infill_density", "filament_prime_volume", "machine_start_gcode"],
            filaments=1,
            wall_loops=5,
            sparse_infill_density="100%",
            machine_start_gcode="G28",
        )

        keys = [o.key for o in overrides_from_config(cfg)]

        assert keys == ["sparse_infill_density", "wall_loops"]
        assert "machine_start_gcode" not in keys, "that is the PRINTER slot"

    def test_a_length_that_contradicts_the_filament_count_is_refused(self) -> None:
        """The array should be 1 + filaments + 1. A file that disagrees with
        itself is one we do not understand, and guessing the index is how the
        designer's machine G-code would end up in the process slot."""
        cfg = _config(["wall_loops", "machine_start_gcode"], filaments=3, wall_loops=5)

        assert overrides_from_config(cfg) == []

    def test_the_expected_length_is_accepted_at_several_slot_counts(self) -> None:
        for filaments in (1, 2, 3, 4):
            changed = ["wall_loops"] + ["x"] * filaments + ["machine_start_gcode"]
            cfg = _config(changed, filaments=filaments, wall_loops=4)
            assert [o.key for o in overrides_from_config(cfg)] == ["wall_loops"], filaments

    def test_a_key_listed_but_absent_from_the_config_is_skipped(self) -> None:
        """Slicers rename keys between versions; there is nothing to carry."""
        cfg = _config(["wall_loops;renamed_away", "f", "p"], filaments=1, wall_loops=5)

        assert [o.key for o in overrides_from_config(cfg)] == ["wall_loops"]

    def test_a_file_without_the_field_offers_nothing(self) -> None:
        assert overrides_from_config({"wall_loops": 5}) == []
        assert overrides_from_config({}) == []
        assert overrides_from_config(None) == []


class TestWhichKeysAreSafe:
    def test_design_intent_is_not_printer_coupled(self) -> None:
        for key in ("wall_loops", "sparse_infill_density", "initial_layer_print_height", "enable_support", "brim_type"):
            assert is_printer_coupled(key) is False, key

    def test_machine_tuned_families_are(self) -> None:
        # Real files carry all of these in the process slot.
        for key in (
            "inner_wall_speed",
            "outer_wall_speed",
            "prime_tower_max_speed",
            "default_acceleration",
            "travel_acceleration",
            "initial_layer_jerk",
            "overhang_fan_speed",
            "nozzle_temperature_initial_layer",
        ):
            assert is_printer_coupled(key) is True, key

    def test_the_flag_reaches_the_caller(self) -> None:
        """The UI ticks intent by default and leaves machine-tuned values for the
        user to opt into — it can only do that if the flag survives."""
        cfg = _config(["wall_loops;inner_wall_speed", "f", "p"], wall_loops=5, inner_wall_speed=200)

        by_key = {o.key: o.printer_coupled for o in overrides_from_config(cfg)}

        assert by_key == {"wall_loops": False, "inner_wall_speed": True}


class TestApplying:
    _OVERRIDES = None

    def _overrides(self):
        cfg = _config(["wall_loops;inner_wall_speed", "f", "p"], wall_loops=5, inner_wall_speed=200)
        return overrides_from_config(cfg)

    def test_only_the_named_keys_are_written(self) -> None:
        """Authoritative selection: offering a key is not choosing it."""
        out = json.loads(apply_design_overrides('{"wall_loops": 2}', self._overrides(), ["wall_loops"]))

        assert out["wall_loops"] == 5
        assert "inner_wall_speed" not in out

    def test_a_key_the_source_does_not_record_is_not_invented(self) -> None:
        out = json.loads(apply_design_overrides('{"wall_loops": 2}', self._overrides(), ["sparse_infill_density"]))

        assert out == {"wall_loops": 2}

    def test_selecting_nothing_leaves_the_profile_untouched(self) -> None:
        assert apply_design_overrides('{"wall_loops": 2}', self._overrides(), []) == '{"wall_loops": 2}'

    def test_unparseable_json_degrades_to_a_plain_slice(self) -> None:
        """A bad input must not fail the slice — it falls back to the picked
        preset, which is exactly the pre-feature behaviour."""
        assert apply_design_overrides("{not json", self._overrides(), ["wall_loops"]) == "{not json"


class TestReadingFromAFile:
    def test_a_real_zip_round_trips(self) -> None:
        cfg = _config(["wall_loops", "f", "p"], wall_loops=5)

        assert [o.key for o in extract_design_process_overrides(_three_mf(cfg))] == ["wall_loops"]

    def test_a_file_that_is_not_a_zip_offers_nothing(self) -> None:
        assert extract_design_process_overrides(b"not a zip") == []

    def test_a_zip_without_project_settings_offers_nothing(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")

        assert extract_design_process_overrides(buf.getvalue()) == []
