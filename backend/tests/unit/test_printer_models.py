"""Unit tests for printer model utilities."""

import pytest

from backend.app.services.camera import get_camera_port, supports_rtsp
from backend.app.utils.printer_models import (
    CARBON_ROD_MODELS,
    LINEAR_RAIL_MODELS,
    STEEL_ROD_MODELS,
    get_rod_type,
    has_ethernet,
    is_dual_nozzle_model,
    normalize_printer_model,
    normalize_printer_model_id,
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
