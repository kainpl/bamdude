"""Tests for the mirrored-BambuStudio printer-config loader + the device-
calibration availability resolver (backend/app/data/printers/*.json)."""

from __future__ import annotations

import pytest

from backend.app.utils.printer_configs import (
    device_calibration_availability,
    has_remote_storage_toggle,
    load_printer_config,
    resolve_device_calibrations,
)


class TestLoader:
    def test_by_display_short(self):
        cfg = load_printer_config("X2D")
        assert cfg and cfg["display_name"] == "Bambu Lab X2D" and cfg["model_id"] == "N6"

    def test_by_internal_code(self):
        assert load_printer_config("N6")["model_id"] == "N6"

    def test_by_long_form(self):
        assert load_printer_config("Bambu Lab X2D")["model_id"] == "N6"

    def test_p1s_resolves_to_c12_not_x1(self):
        # BS: C12 = P1S. Our stale PRINTER_MODEL_ID_MAP says C12=X1; the loader
        # ignores it and keys off the JSON display_name, so P1S -> C12 correctly.
        cfg = load_printer_config("P1S")
        assert cfg and cfg["model_id"] == "C12" and cfg["display_name"] == "Bambu Lab P1S"

    def test_p1p_resolves_to_c11(self):
        cfg = load_printer_config("P1P")
        assert cfg and cfg["model_id"] == "C11"

    def test_h2c_alt_code(self):
        # O1C and O1C2 both = H2C — either resolves.
        assert load_printer_config("H2C")["display_name"] == "Bambu Lab H2C"
        assert load_printer_config("O1C2")["model_id"] == "O1C2"

    @pytest.mark.parametrize("model", ["NoSuchModel", "", None])
    def test_unknown_returns_none(self, model):
        assert load_printer_config(model) is None


class TestDeviceCalibrationAvailability:
    def test_x2d_full(self):
        assert device_calibration_availability("X2D") == {
            "lidar": False,
            "bed_leveling": True,
            "vibration": True,
            "motor_noise": False,
            "nozzle_offset": True,
            "high_temp_heatbed": True,
            "clump_pos": False,
        }

    @pytest.mark.parametrize("model", ["P1S", "A1 Mini", "X1C", "X2D", "P2S", "H2S", "H2D", "UnknownModel"])
    def test_bed_and_vibration_always(self, model):
        a = device_calibration_availability(model)
        assert a["bed_leveling"] is True and a["vibration"] is True

    def test_p2s_clump_no_nozzle(self):
        a = device_calibration_availability("P2S")
        assert a["clump_pos"] is True and a["high_temp_heatbed"] is True and a["nozzle_offset"] is False

    def test_h2d_nozzle_no_clump(self):
        a = device_calibration_availability("H2D")
        assert a["nozzle_offset"] is True and a["clump_pos"] is False

    def test_h2s_clump(self):
        assert device_calibration_availability("H2S")["clump_pos"] is True

    def test_unknown_model_conservative(self):
        # No config → only the two universal calibrations offered.
        a = device_calibration_availability("TotallyUnknown")
        assert a == {
            "lidar": False,
            "bed_leveling": True,
            "vibration": True,
            "motor_noise": False,
            "nozzle_offset": False,
            "high_temp_heatbed": False,
            "clump_pos": False,
        }


class TestHybridResolver:
    def test_no_live_returns_base(self):
        assert resolve_device_calibrations("X2D") == device_calibration_availability("X2D")

    def test_live_enables_motor_noise(self):
        # X2D base motor_noise=False; a printer reporting it True overrides (firmware-live).
        assert resolve_device_calibrations("X2D", {"support_motor_noise_cali": True})["motor_noise"] is True

    def test_live_disables_nozzle(self):
        assert (
            resolve_device_calibrations("H2D", {"support_nozzle_offset_calibration": False})["nozzle_offset"] is False
        )

    def test_live_clump_and_bed_off(self):
        r = resolve_device_calibrations("X2D", {"support_clump_position_calibration": True, "support_bed_leveling": 0})
        assert r["clump_pos"] is True and r["bed_leveling"] is False

    def test_live_lidar_needs_both(self):
        # Reporting lidar_cali True but ai_monitoring False → lidar stays False.
        r = resolve_device_calibrations("X1C", {"support_lidar_calibration": True, "support_ai_monitoring": False})
        assert r["lidar"] is False


class TestRemoteStorageToggle:
    """Reachability of the "Store sent files on external storage" toggle (#2524).

    BS renders the toggle only for models declaring
    ``support_save_remote_print_file_to_storage``; the external_storage
    diagnostic skips instead of failing on the rest.
    """

    @pytest.mark.parametrize("model", ["X1C", "X2D", "P2S", "H2D", "H2S", "BL-P001", "N6"])
    def test_declared_models_have_the_toggle(self, model):
        assert has_remote_storage_toggle(model) is True

    @pytest.mark.parametrize("model", ["P1S", "P1P", "C11", "C12"])
    def test_p1_series_has_no_toggle(self, model):
        # Upstream's hardcoded set, resolved from the mirrored configs instead.
        assert has_remote_storage_toggle(model) is False

    @pytest.mark.parametrize("model", ["A2L", "X1E"])
    def test_undeclared_models_have_no_toggle(self, model):
        # BamDude divergence: config-driven, so it also covers models upstream's
        # {P1S, P1P} list misses.
        assert has_remote_storage_toggle(model) is False

    def test_unknown_model_defaults_open(self):
        # Nothing mirrored → keep the check working rather than silently skip.
        assert has_remote_storage_toggle("TotallyUnknown") is True
        assert has_remote_storage_toggle(None) is True

    def test_live_capability_wins_over_config(self):
        # A firmware that starts reporting it reactivates the check; one that
        # reports it off suppresses the check even on a declaring model.
        assert has_remote_storage_toggle("P1S", {"save_remote_to_storage": True}) is True
        assert has_remote_storage_toggle("X1C", {"save_remote_to_storage": False}) is False

    def test_unreported_live_key_falls_back_to_config(self):
        # A sparse push (other options only) must not be read as "not supported".
        assert has_remote_storage_toggle("X1C", {"sound": True}) is True
        assert has_remote_storage_toggle("P1S", {"sound": True}) is False
