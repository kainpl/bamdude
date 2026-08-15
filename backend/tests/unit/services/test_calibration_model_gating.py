"""Which calibrations a printer is offered, and where that answer comes from.

BamDude showed every filament calibration to every machine except the two
automatic ones, and those it gated on a hardcoded list of model names —
``{X1, X1C, X1E, H2D, H2DPRO}``. A list of names cannot know about a machine
released after it was written, and this one was wrong in both directions at
once: it claimed lidar on the H2D, whose own BS config says otherwise, and it
had never heard of the X2D, P2S, A2L, H2C or H2S.

⚠️ **Two mechanisms answer the same question.** The X1 family runs auto PA and
auto flow off the lidar; the H2 / X2D / P2S / A2L generation has no lidar and
uses ``support_auto_flow_calibration``. Neither flag alone describes the fleet,
so the base is their union — read from the mirrored BambuStudio config, which is
where a model released next year arrives on its own.

The tests run against the SHIPPED config files, not fixtures: a re-sync from a
new BS tag that changes a model's capabilities should show up here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.printer_capabilities import compute_calibration_supports
from backend.app.utils.printer_configs import filament_calibration_availability


def _state(**overrides):
    base = {
        "firmware_version": None,
        "is_support_pa_calibration": True,
        "is_support_auto_flow_calibration": True,
        "nozzles": [],
        "print_option_support": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestTheBaseComesFromTheConfig:
    @pytest.mark.parametrize("model", ["X1C", "X1E"])
    def test_the_lidar_generation_qualifies(self, model: str) -> None:
        assert filament_calibration_availability(model)["pa_auto"] is True

    @pytest.mark.parametrize("model", ["X2D", "P2S", "A2L", "H2C", "H2S", "H2D", "H2D Pro"])
    def test_so_does_the_generation_that_has_no_lidar_at_all(self, model: str) -> None:
        """⚠️ Five of these seven were offered no automatic calibration before,
        because the list they were checked against only knew about lidar
        machines. The H2D was in the list — for the wrong reason."""
        assert filament_calibration_availability(model)["pa_auto"] is True

    @pytest.mark.parametrize("model", ["P1S", "P1P", "A1", "A1 mini"])
    def test_and_the_machines_with_neither_do_not(self, model: str) -> None:
        assert filament_calibration_availability(model)["pa_auto"] is False

    def test_an_unknown_model_claims_nothing(self, model: str = "Bambu Lab Q9") -> None:
        """A model with no mirrored config answers "no" rather than guessing.
        Hiding a row that would work is recoverable; offering one that cannot is
        the promise this whole task exists to stop making."""
        assert filament_calibration_availability(model) == {"pa_auto": False, "flow_auto": False}

    def test_a_missing_model_is_not_an_error(self) -> None:
        assert filament_calibration_availability(None)["flow_auto"] is False


class TestThePrinterStillHasTheLastWord:
    def test_a_capable_model_that_reports_nothing_is_offered_nothing(self) -> None:
        """⚠️ The AND is load-bearing. The push flags carry BS's two series
        clamps for firmware that advertises what it does not have (grep
        _apply_series_calibration_clamps), so a base that overrode them would
        re-enable exactly what Bambu refuses to believe."""
        caps = compute_calibration_supports(
            _state(is_support_pa_calibration=False, is_support_auto_flow_calibration=False), "X2D"
        )

        assert caps["pa_auto"] is False
        assert caps["flow_auto"] is False

    def test_the_two_rows_are_gated_separately(self) -> None:
        """The H2 family is exactly this case: BS clamps its auto flow off while
        leaving auto PA alone."""
        caps = compute_calibration_supports(_state(is_support_auto_flow_calibration=False), "H2D")

        assert caps["pa_auto"] is True
        assert caps["flow_auto"] is False

    def test_a_reporting_printer_of_an_incapable_model_is_still_refused(self) -> None:
        caps = compute_calibration_supports(_state(), "A1 mini")

        assert caps["pa_auto"] is False


class TestTheManualRowsStayUniversal:
    @pytest.mark.parametrize("model", ["A1 mini", "X1C", "X2D"])
    def test_every_machine_keeps_them(self, model: str) -> None:
        """⚠️ Deliberately NOT gated. BS's own manual dialog offers PA line,
        pattern and tower to any printer — its only branch is DDE vs Bowden,
        which changes the parameter range, not the availability. Towers are just
        prints. Hiding one here would be BamDude inventing a restriction and
        calling it parity.
        """
        caps = compute_calibration_supports(_state(), model)

        assert caps["pa_manual"] is True
        assert caps["flow_manual"] is True
        assert all(caps[k] for k in ("temp_tower", "vol_speed_tower", "vfa_tower", "retraction_tower"))


class TestExtruderLayout:
    @pytest.mark.parametrize("model", ["X2D", "H2C", "H2D", "H2D Pro"])
    def test_a_two_nozzle_machine_is_reported_as_one(self, model: str) -> None:
        """⚠️ There was a second, smaller list of dual-nozzle models here —
        {H2D, H2DPRO} — beside a canonical one the same file already imported.
        The X2D and H2C fell through it, so the wizard showed a single extruder
        on a two-nozzle machine and auto PA line picked the single-extruder
        asset for it."""
        caps = compute_calibration_supports(_state(), model)

        assert caps["dual_extruder"] is True
        assert [e["name"] for e in caps["extruders"]] == ["Right", "Left"]

    @pytest.mark.parametrize("model", ["X1C", "P1S", "A1", "P2S"])
    def test_a_single_nozzle_machine_gets_one_unnamed_extruder(self, model: str) -> None:
        caps = compute_calibration_supports(_state(), model)

        assert caps["dual_extruder"] is False
        assert [e["name"] for e in caps["extruders"]] == ["Main"]
