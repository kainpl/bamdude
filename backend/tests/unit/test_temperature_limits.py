"""How hot a thing may be told to get — the rule, without a printer attached.

Registry N6. BambuStudio spreads this across three files and the pieces answer
three different questions; copying any one of them alone gets it wrong.

⚠️ **The bed's ceiling is LOWER at 220 V.** ``get_bed_temperature_limit()``
returns 110 on the X1 and O series when the mains bit is set, 120 otherwise. It
reads backwards until you take it as a fact about the heating element rather
than about available power — and the bit is ``home_flag`` bit 3, which we did
not read at all.

⚠️ **Only the bed's MAXIMUM is ever overridden.** ``update_temp_ctrl`` calls
``SetMaxTemp`` for the bed and leaves its floor alone, while for the nozzle it
replaces both ends — and only when the printer sent at least two values.
Copying the nozzle's shape onto the bed would move a floor BS does not move.

⚠️ **Zero belongs to no range and must always pass.** It is how each of these is
switched off. BS exempts it with ``AddTemp(0)``, which lifts a value out of both
the too-high and the too-low check. A floor enforced without that exemption
turns "stop heating" into "heat to 20", which is the opposite request.
"""

from __future__ import annotations

import pytest

from backend.app.utils.temperature_limits import (
    BED_RANGE_DEFAULT,
    CHAMBER_RANGE_DEFAULT,
    NOZZLE_RANGE_DEFAULT,
    OFF,
    bed_limits,
    chamber_limits,
    clamp_target,
    is_within,
    nozzle_limits,
)


class TestTheNozzleRange:
    def test_without_a_report_it_is_bs_static_pair(self) -> None:
        assert nozzle_limits(None) == NOZZLE_RANGE_DEFAULT == (20, 300)

    def test_a_reported_range_replaces_both_ends(self) -> None:
        """``if (obj->nozzle_temp_range.size() >= 2)`` sets min AND max."""
        assert nozzle_limits([170, 320]) == (170, 320)

    @pytest.mark.parametrize("reported", [[], [250], "300", None, [250, "320"], [300, 100]])
    def test_anything_unusable_falls_back(self, reported) -> None:
        """⚠️ BS guards on the size of a PARSED vector, so a range it could not
        parse is indistinguishable from one that never arrived. Ours has to
        collapse the same way or it would trust a half-formed pair."""
        assert nozzle_limits(reported) == NOZZLE_RANGE_DEFAULT


class TestTheBedCeiling:
    def test_the_static_default(self) -> None:
        assert bed_limits() == BED_RANGE_DEFAULT == (20, 120)

    @pytest.mark.parametrize("series", ["series_x1", "series_o"])
    def test_220v_lowers_it(self, series: str) -> None:
        """⚠️ The counterintuitive one, and it covers the O series too because
        ``get_printer_series()`` folds both onto ``SERIES_X1``."""
        assert bed_limits(series=series, is_220v=True)[1] == 110
        assert bed_limits(series=series, is_220v=False)[1] == 120

    def test_the_voltage_rule_does_not_reach_other_series(self) -> None:
        """A P1 at 220 V keeps 120 — the rule is series-first, voltage-second."""
        assert bed_limits(series="series_p1p", is_220v=True)[1] == 120

    def test_a_reported_limit_is_used_off_the_voltage_path(self) -> None:
        assert bed_limits(series="series_p1p", reported_limit=100)[1] == 100

    def test_a_negative_reported_limit_means_absent(self) -> None:
        """BS: ``bed_temperature_limit < 0 ? BED_TEMP_LIMIT : …`` — the field
        initialises to -1 and that is how "not reported" is spelled."""
        assert bed_limits(series="series_p1p", reported_limit=-1)[1] == 120

    def test_a_reported_range_beats_everything(self) -> None:
        """Applied last in ``update_temp_ctrl``, so it wins over the voltage
        rule that was computed a line earlier."""
        assert bed_limits(series="series_x1", is_220v=True, reported_range=[20, 130])[1] == 130

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"series": "series_x1", "is_220v": True},
            {"reported_limit": 100},
            {"reported_range": [40, 130]},
        ],
    )
    def test_the_floor_never_moves(self, kwargs) -> None:
        """⚠️ Not even when a reported range names a different one. BS sets only
        ``SetMaxTemp`` for the bed."""
        assert bed_limits(**kwargs)[0] == BED_RANGE_DEFAULT[0]


class TestTheChamberRange:
    def test_from_the_mirrored_config(self) -> None:
        assert chamber_limits([0, 65]) == (0, 65)

    def test_the_fallback_is_devconfigs_not_statuspanels(self) -> None:
        """⚠️ 0-60, not StatusPanel's 20-60. That pair is only the placeholder
        shown before a printer answers; ``update_temp_ctrl`` replaces it with
        DevConfig's own defaults, which floor at 0."""
        assert chamber_limits(None) == CHAMBER_RANGE_DEFAULT == (0, 60)


class TestZeroIsAlwaysAllowed:
    @pytest.mark.parametrize("limits", [(20, 300), (20, 120), (0, 65), (40, 60)])
    def test_off_passes_every_range(self, limits) -> None:
        """⚠️ The whole point of ``AddTemp(0)``. A floor of 20 enforced without
        this exemption would turn "stop heating" into "heat to 20"."""
        assert is_within(OFF, limits) is True
        assert clamp_target(OFF, limits) == OFF


class TestClampingCutsTheTopOnly:
    def test_it_cuts_an_over_range_request_down(self) -> None:
        assert clamp_target(400, (20, 300)) == 300

    def test_it_leaves_an_in_range_request_alone(self) -> None:
        assert clamp_target(250, (20, 300)) == 250

    def test_it_does_not_raise_a_low_request(self) -> None:
        """⚠️ BS's send path clamps the maximum and says nothing about the
        minimum — the input widget already refused anything below it. Raising a
        low value here would invent a heat request BS never makes."""
        assert clamp_target(5, (20, 300)) == 5


class TestIsWithinIsTheOtherQuestion:
    """The widget's question, kept apart from the send path's on purpose: a
    caller deserves to be told its request was out of range, and the wire still
    gets something that cannot cook anything if the telling is skipped."""

    def test_it_refuses_both_ends(self) -> None:
        assert is_within(5, (20, 300)) is False
        assert is_within(400, (20, 300)) is False

    def test_it_accepts_the_boundaries(self) -> None:
        assert is_within(20, (20, 300)) is True
        assert is_within(300, (20, 300)) is True
