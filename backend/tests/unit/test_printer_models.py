"""Unit tests for printer model utilities."""

import pytest

from backend.app.services.camera import get_camera_port, supports_rtsp
from backend.app.utils.printer_models import (
    CARBON_ROD_MODELS,
    LINEAR_RAIL_MODELS,
    STEEL_ROD_MODELS,
    get_rod_type,
    has_ethernet,
    has_external_storage,
    is_dual_nozzle_model,
    normalize_printer_model,
    normalize_printer_model_id,
    supports_auto_bed_leveling,
    supports_auto_flow_cali,
    supports_auto_nozzle_offset,
)


class TestIsDualNozzleModel:
    """Tests for is_dual_nozzle_model() nozzle-class classification."""

    # Takes an already-normalized short code / display name (callers run
    # normalize_printer_model first), so "H2D" matches but "Bambu Lab H2D"
    # does not — same contract as has_ethernet / get_rod_type.
    @pytest.mark.parametrize("model", ["H2D", "h2d", "H2D Pro", "H2C", "X2D", "O1D", "N6"])
    def test_dual_nozzle_models(self, model):
        assert is_dual_nozzle_model(model) is True

    @pytest.mark.parametrize("model", ["X1C", "P1S", "A1", "A1 mini", None, ""])
    def test_single_nozzle_models(self, model):
        assert is_dual_nozzle_model(model) is False


class TestGetRodType:
    """Tests for get_rod_type() rod/rail classification."""

    @pytest.mark.parametrize("model", ["X1C", "X1", "X1E", "P1P", "P1S"])
    def test_carbon_rod_models(self, model: str):
        assert get_rod_type(model) == "carbon"

    @pytest.mark.parametrize("model", ["C11", "C12", "C13"])
    def test_carbon_rod_internal_codes(self, model: str):
        assert get_rod_type(model) == "carbon"

    def test_p2s_is_steel_rod(self):
        """P2S uses hardened steel rods, not carbon rods (#640)."""
        assert get_rod_type("P2S") == "steel_rod"

    def test_p2s_internal_code_is_steel_rod(self):
        """N7 (P2S internal code) uses steel rods."""
        assert get_rod_type("N7") == "steel_rod"

    @pytest.mark.parametrize("model", ["A1", "A1 Mini", "H2D", "H2D Pro", "H2C", "H2S"])
    def test_linear_rail_models(self, model: str):
        assert get_rod_type(model) == "linear_rail"

    @pytest.mark.parametrize("model", ["N1", "N2S", "A11", "A12", "O1D", "O1E", "O2D", "O1C", "O1C2", "O1S"])
    def test_linear_rail_internal_codes(self, model: str):
        assert get_rod_type(model) == "linear_rail"

    def test_unknown_model_returns_none(self):
        assert get_rod_type("UNKNOWN") is None

    def test_none_returns_none(self):
        assert get_rod_type(None) is None

    def test_case_insensitive(self):
        assert get_rod_type("p2s") == "steel_rod"
        assert get_rod_type("x1c") == "carbon"
        assert get_rod_type("a1") == "linear_rail"

    def test_strips_whitespace_and_dashes(self):
        assert get_rod_type(" P2S ") == "steel_rod"
        assert get_rod_type("A1-Mini") == "linear_rail"


class TestX2DModel:
    """X2D printer support (issue #988).

    The X2D is a dual-nozzle enclosed printer launched April 2026. It shares
    the hardened steel rod hardware with P2S (NOT carbon rods) and uses
    RTSP on port 322 like other X/H series printers. Internal SSDP/MQTT
    model code is "N6"; serial numbers begin with "20P9".
    """

    def test_x2d_is_steel_rod_display_name(self):
        assert get_rod_type("X2D") == "steel_rod"

    def test_x2d_is_steel_rod_internal_code(self):
        assert get_rod_type("N6") == "steel_rod"

    def test_x2d_model_id_map(self):
        assert normalize_printer_model_id("N6") == "X2D"

    def test_x2d_model_map(self):
        assert normalize_printer_model("Bambu Lab X2D") == "X2D"

    def test_x2d_has_ethernet_display_name(self):
        assert has_ethernet("X2D") is True

    def test_x2d_has_ethernet_internal_code(self):
        assert has_ethernet("N6") is True

    def test_x2d_supports_rtsp_display_name(self):
        assert supports_rtsp("X2D") is True

    def test_x2d_supports_rtsp_internal_code(self):
        assert supports_rtsp("N6") is True

    def test_x2d_camera_port_is_rtsp(self):
        assert get_camera_port("N6") == 322
        assert get_camera_port("X2D") == 322

    def test_x2d_not_in_carbon_rod_set(self):
        """Regression guard: X2D has hardened steel rods, not carbon (#988).

        Pins the classification so a future change that reverts it fails loudly.
        """
        assert "X2D" not in CARBON_ROD_MODELS
        assert "N6" not in CARBON_ROD_MODELS
        assert "X2D" in STEEL_ROD_MODELS
        assert "N6" in STEEL_ROD_MODELS


class TestA2LModel:
    """A2L printer support (#1684).

    Hybrid 3D printer + cutter/plotter. Linear rails like the A1 family, NO
    Ethernet (Wi-Fi 2.4 GHz only), low-rate chamber-image camera on port 6000
    (no RTSP), single FDM extruder (the second "tool head" in BambuStudio's
    profile is the cutter, not a second extruder — must NOT be dual-nozzle).
    Internal SSDP/MQTT code "N9"; serials begin "26A19".
    """

    def test_a2l_is_linear_rail_display_name(self):
        assert get_rod_type("A2L") == "linear_rail"

    def test_a2l_is_linear_rail_internal_code(self):
        assert get_rod_type("N9") == "linear_rail"

    def test_a2l_model_id_map(self):
        assert normalize_printer_model_id("N9") == "A2L"

    def test_a2l_model_map(self):
        assert normalize_printer_model("Bambu Lab A2L") == "A2L"

    def test_a2l_has_no_ethernet_display_name(self):
        assert has_ethernet("A2L") is False

    def test_a2l_has_no_ethernet_internal_code(self):
        assert has_ethernet("N9") is False

    def test_a2l_does_not_support_rtsp_display_name(self):
        assert supports_rtsp("A2L") is False

    def test_a2l_does_not_support_rtsp_internal_code(self):
        assert supports_rtsp("N9") is False

    def test_a2l_camera_port_is_chamber_image(self):
        assert get_camera_port("A2L") == 6000
        assert get_camera_port("N9") == 6000

    def test_a2l_is_not_dual_nozzle(self):
        """Single FDM extruder + cutter head — must not land in the dual-nozzle
        group or AMS routing targets the deputy slot (firmware rejects 07FF_8012)."""
        assert is_dual_nozzle_model("A2L") is False
        assert is_dual_nozzle_model("N9") is False

    def test_a2l_in_linear_rail_set(self):
        assert "A2L" in LINEAR_RAIL_MODELS
        assert "N9" in LINEAR_RAIL_MODELS

    def test_a2l_not_in_carbon_or_steel_rod_sets(self):
        assert "A2L" not in CARBON_ROD_MODELS
        assert "N9" not in CARBON_ROD_MODELS
        assert "A2L" not in STEEL_ROD_MODELS
        assert "N9" not in STEEL_ROD_MODELS


class TestA1SeriesModelIds:
    """Regression guard for the A1-family internal-code → display-name map.

    N1 = A1 Mini, N2S = A1 — every other registry agrees; printer_models.py was
    the lone outlier that had them flipped (fixed while scoping A2L, #1684). Pin
    both directions so a future re-flip fails loudly.
    """

    def test_n2s_is_a1(self):
        assert normalize_printer_model_id("N2S") == "A1"

    def test_n1_is_a1_mini(self):
        assert normalize_printer_model_id("N1") == "A1 Mini"


class TestHasExternalStorage:
    """Pins which Bambu models have a MicroSD slot. The connection
    diagnostic flips its ``external_storage`` check from ``fail`` to
    ``skip`` based on this — a false add (X1C marked as no-storage) would
    silently disable a genuine fail signal for X1/P1/P2S/H2 users (#1703)."""

    @pytest.mark.parametrize("model", ["A1", "A1 Mini", "A1MINI", "A1-Mini", "a1"])
    def test_a1_series_has_no_external_storage(self, model: str):
        assert has_external_storage(model) is False

    @pytest.mark.parametrize("model", ["N1", "N2S", "A04", "A11", "A12"])
    def test_a1_internal_codes_have_no_external_storage(self, model: str):
        assert has_external_storage(model) is False

    @pytest.mark.parametrize(
        "model",
        ["X1C", "X1E", "X1", "P1S", "P1P", "P2S", "H2D", "H2D Pro", "H2C", "H2S", "X2D"],
    )
    def test_other_models_have_external_storage(self, model: str):
        assert has_external_storage(model) is True

    def test_unknown_model_defaults_to_true(self):
        # Default-true keeps the diagnostic active for new Bambu models;
        # add them to NO_EXTERNAL_STORAGE_MODELS explicitly when they ship
        # without a slot.
        assert has_external_storage("BrandNewModel2027") is True

    def test_none_and_empty_default_to_true(self):
        assert has_external_storage(None) is True
        assert has_external_storage("") is True


class TestAutoCalibrationCapabilities:
    """Which models advertise an *auto* calibration mode → the print dialog's
    3-position off/auto/on control. Source: BambuStudio resources/printers/*.json.

    Bed + flow auto: {A2L, P2S, H2S, H2C, H2D, H2D Pro, X2D} (independent of
    nozzle count). Nozzle-offset auto: dual-nozzle only {H2C, H2D, H2D Pro, X2D}.
    """

    # ---- bed + flow (same matrix) ----
    @pytest.mark.parametrize("model", ["A2L", "P2S", "H2S", "H2C", "H2D", "H2D Pro", "X2D"])
    def test_bed_and_flow_auto_display_names(self, model: str):
        assert supports_auto_bed_leveling(model) is True
        assert supports_auto_flow_cali(model) is True

    @pytest.mark.parametrize("model", ["N9", "N7", "O1S", "O1C", "O1C2", "O1D", "O1E", "O2D", "N6"])
    def test_bed_and_flow_auto_internal_codes(self, model: str):
        assert supports_auto_bed_leveling(model) is True
        assert supports_auto_flow_cali(model) is True

    @pytest.mark.parametrize("model", ["X1C", "X1", "X1E", "P1S", "P1P", "A1", "A1 Mini", None, ""])
    def test_bed_and_flow_no_auto(self, model):
        assert supports_auto_bed_leveling(model) is False
        assert supports_auto_flow_cali(model) is False

    # ---- nozzle-offset (dual-nozzle only) ----
    @pytest.mark.parametrize("model", ["H2C", "H2D", "H2D Pro", "X2D", "O1C", "O1D", "O1E", "O2D", "N6"])
    def test_nozzle_offset_auto(self, model: str):
        assert supports_auto_nozzle_offset(model) is True

    @pytest.mark.parametrize("model", ["A2L", "P2S", "H2S", "N9", "N7", "O1S"])
    def test_bed_flow_auto_but_not_nozzle_offset(self, model: str):
        """Single-nozzle auto-capable models advertise bed/flow auto but NOT
        nozzle-offset auto (a nozzle offset only exists with two nozzles)."""
        assert supports_auto_bed_leveling(model) is True
        assert supports_auto_nozzle_offset(model) is False

    @pytest.mark.parametrize("model", ["X1C", "P1S", "A1", "A1 Mini", None, ""])
    def test_single_nozzle_no_nozzle_offset_auto(self, model):
        assert supports_auto_nozzle_offset(model) is False

    def test_case_and_dash_insensitive(self):
        assert supports_auto_bed_leveling("h2d pro") is True
        assert supports_auto_bed_leveling(" H2D-Pro ") is True
        assert supports_auto_nozzle_offset("x2d") is True
