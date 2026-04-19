"""Unit tests for maintenance rod-type filtering logic."""

import pytest

from backend.app.api.routes.maintenance import _should_apply_to_printer


class TestShouldApplyToPrinter:
    """Tests for _should_apply_to_printer() model-specific filtering."""

    # Carbon rod tasks should only apply to X1/P1 models
    @pytest.mark.parametrize("model", ["X1C", "X1", "X1E", "P1P", "P1S"])
    def test_carbon_rod_tasks_apply_to_carbon_models(self, model: str):
        assert _should_apply_to_printer("clean_carbon_rods", model) is True

    def test_carbon_rod_tasks_do_not_apply_to_p2s(self):
        """P2S has steel rods, not carbon rods (#640)."""
        assert _should_apply_to_printer("clean_carbon_rods", "P2S") is False

    def test_carbon_rod_tasks_do_not_apply_to_a1(self):
        assert _should_apply_to_printer("clean_carbon_rods", "A1") is False

    # Steel rod tasks should only apply to P2S
    def test_steel_rod_tasks_apply_to_p2s(self):
        assert _should_apply_to_printer("lubricate_steel_rods", "P2S") is True
        assert _should_apply_to_printer("clean_steel_rods", "P2S") is True

    def test_steel_rod_tasks_do_not_apply_to_x1c(self):
        assert _should_apply_to_printer("lubricate_steel_rods", "X1C") is False
        assert _should_apply_to_printer("clean_steel_rods", "X1C") is False

    def test_steel_rod_tasks_do_not_apply_to_a1(self):
        assert _should_apply_to_printer("lubricate_steel_rods", "A1") is False

    # Linear rail tasks should only apply to A1/H2 models
    @pytest.mark.parametrize("model", ["A1", "A1 Mini", "H2D", "H2C", "H2S"])
    def test_linear_rail_tasks_apply_to_rail_models(self, model: str):
        assert _should_apply_to_printer("lubricate_linear_rails", model) is True
        assert _should_apply_to_printer("clean_linear_rails", model) is True

    def test_linear_rail_tasks_do_not_apply_to_p2s(self):
        assert _should_apply_to_printer("lubricate_linear_rails", "P2S") is False

    # Universal tasks apply to all models
    @pytest.mark.parametrize("model", ["X1C", "P2S", "A1", "H2D"])
    def test_universal_tasks_apply_to_all(self, model: str):
        assert _should_apply_to_printer("clean_nozzle", model) is True
        assert _should_apply_to_printer("check_belt_tension", model) is True

    # Unknown models default to carbon (legacy behavior)
    def test_unknown_model_defaults_to_carbon(self):
        assert _should_apply_to_printer("clean_carbon_rods", "UNKNOWN") is True
        assert _should_apply_to_printer("lubricate_steel_rods", "UNKNOWN") is False
        assert _should_apply_to_printer("lubricate_linear_rails", "UNKNOWN") is False

    # None/custom type_code applies to all
    def test_none_type_code_applies_to_all(self):
        assert _should_apply_to_printer(None, "X1C") is True

    def test_custom_type_code_applies_to_all(self):
        assert _should_apply_to_printer("custom_42", "P2S") is True
