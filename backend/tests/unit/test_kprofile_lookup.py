"""An AMS slot's K value, resolved against the nozzle that slot feeds.

H2-series trays carry no ``k`` of their own — only ``cali_idx`` — so the card
looks the index up in ``state.kprofiles``. That table is numbered per nozzle
(and, on a dual-nozzle machine, per hotend), so the index alone does not name
one profile: the WebSocket shaper keyed on it anyway and printed whichever
nozzle's entry was listed last. Ported from upstream #2854 + #3044.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.bambu_mqtt import KProfile
from backend.app.utils.kprofile_lookup import build_slot_k_resolver


def _state(profiles, *, nozzles=("0.4",), ams_extruder_map=None):
    return SimpleNamespace(
        kprofiles=list(profiles),
        nozzles=[SimpleNamespace(nozzle_diameter=d) for d in nozzles],
        ams_extruder_map=ams_extruder_map or {},
    )


def _profile(cali_idx: int, k_value: str, nozzle: str, extruder: int = 0) -> KProfile:
    return KProfile(
        slot_id=cali_idx,
        extruder_id=extruder,
        nozzle_id="",
        nozzle_diameter=nozzle,
        filament_id="GFL99",
        name=f"Profile {cali_idx}",
        k_value=k_value,
    )


class TestTheSlotsOwnNozzle:
    def test_a_slot_reads_the_profile_on_its_own_extruder(self):
        """The H2C case: one spool, 0.018 on the left hotend, 0.020 on the right,
        both under index 3. AMS 0 feeds extruder 1, AMS 1 feeds extruder 0."""
        resolve = build_slot_k_resolver(
            _state(
                [_profile(3, "0.020000", "0.4", extruder=0), _profile(3, "0.018000", "0.4", extruder=1)],
                ams_extruder_map={"0": 1, "1": 0},
            )
        )

        assert resolve(3, 0, 0) == pytest.approx(0.018)
        assert resolve(3, 1, 0) == pytest.approx(0.020)

    def test_a_swapped_out_nozzles_table_loses_to_the_fitted_one(self):
        """One extruder, two diameters' tables: only one of them is installed."""
        resolve = build_slot_k_resolver(
            _state([_profile(3, "0.020000", "0.4"), _profile(3, "0.017000", "0.6")], nozzles=("0.6",))
        )

        assert resolve(3, 0, 0) == pytest.approx(0.017)

    def test_an_index_two_fitted_nozzles_both_claim_reads_as_unknown(self):
        """A blank beats confidently printing the other nozzle's number."""
        resolve = build_slot_k_resolver(
            _state([_profile(3, "0.020000", "0.4"), _profile(3, "0.017000", "0.6")], nozzles=("0.4", "0.6"))
        )

        assert resolve(3, 0, 0) is None

    def test_a_single_nozzle_printer_resolves_without_an_extruder_map(self):
        resolve = build_slot_k_resolver(_state([_profile(3, "0.020000", "0.4")]))

        assert resolve(3, 0, 2) == pytest.approx(0.020)

    def test_an_uncalibrated_slot_has_no_value(self):
        resolve = build_slot_k_resolver(_state([_profile(3, "0.020000", "0.4")]))

        assert resolve(None, 0, 0) is None
        assert resolve(-1, 0, 0) is None

    def test_an_unparseable_k_value_is_skipped_rather_than_raising(self):
        resolve = build_slot_k_resolver(_state([_profile(3, "not-a-number", "0.4")]))

        assert resolve(3, 0, 0) is None

    def test_an_index_no_profile_holds_is_nothing(self):
        resolve = build_slot_k_resolver(_state([_profile(3, "0.020000", "0.4", extruder=0)], ams_extruder_map={"0": 0}))

        assert resolve(9, 0, 0) is None


class TestAPrinterThatDoesNotFilePerHotend:
    """#3044: an X2D showed K on its first AMS and nothing on its second. The
    second AMS's slots pointed at the same entries as the first, and those
    entries carry one extruder — requiring a match found nothing."""

    def test_a_shared_profile_resolves_on_the_other_extruder(self):
        resolve = build_slot_k_resolver(
            _state([_profile(3, "0.021000", "0.4", extruder=0)], ams_extruder_map={"0": 0, "1": 1})
        )

        assert resolve(3, 0, 0) == pytest.approx(0.021)
        assert resolve(3, 1, 0) == pytest.approx(0.021)

    def test_two_entries_agreeing_on_one_k_is_not_an_ambiguity(self):
        resolve = build_slot_k_resolver(
            _state(
                [_profile(3, "0.021000", "0.4", extruder=0), _profile(3, "0.021000", "0.6", extruder=0)],
                nozzles=("0.4", "0.6"),
                ams_extruder_map={"0": 1},
            )
        )

        assert resolve(3, 0, 0) == pytest.approx(0.021)

    def test_the_fallback_still_prefers_the_nozzle_that_is_fitted(self):
        resolve = build_slot_k_resolver(
            _state(
                [_profile(3, "0.020000", "0.4", extruder=0), _profile(3, "0.017000", "0.6", extruder=0)],
                nozzles=("0.6",),
                ams_extruder_map={"0": 1},
            )
        )

        assert resolve(3, 0, 0) == pytest.approx(0.017)

    def test_the_fallback_refuses_when_the_candidates_disagree(self):
        resolve = build_slot_k_resolver(
            _state(
                [_profile(3, "0.020000", "0.4", extruder=0), _profile(3, "0.017000", "0.6", extruder=0)],
                nozzles=("0.4", "0.6"),
                ams_extruder_map={"0": 1},
            )
        )

        assert resolve(3, 0, 0) is None

    def test_a_hotend_with_its_own_profiles_does_not_borrow_the_others(self):
        """The H2C guard the fallback must not reopen: a right-hand slot bound to
        16 means entry 16 of the RIGHT nozzle's table; the left's 16 is a
        different profile, not a stand-in."""
        resolve = build_slot_k_resolver(
            _state(
                [_profile(16, "0.018000", "0.4", extruder=1), _profile(15, "0.020000", "0.4", extruder=0)],
                ams_extruder_map={"0": 0, "1": 1},
            )
        )

        assert resolve(16, 0, 0) is None
        assert resolve(15, 0, 0) == pytest.approx(0.020)
        assert resolve(16, 1, 0) == pytest.approx(0.018)


class TestRoutingWeCannotKnow:
    def test_an_ams_the_map_does_not_name_is_not_filed_under_extruder_0(self):
        """A dual-nozzle AMS reporting 0xE (uninitialised, or behind a Filament
        Track Switch) has no extruder in the map. Defaulting it to extruder 0
        would print the right hotend's K on a slot that may feed the left, so
        it takes an answer only where every extruder agrees."""
        resolve = build_slot_k_resolver(
            _state(
                [_profile(3, "0.020000", "0.4", extruder=0), _profile(3, "0.018000", "0.4", extruder=1)],
                ams_extruder_map={"0": 1},
            )
        )

        assert resolve(3, 2, 0) is None

    def test_an_unrouted_slot_still_resolves_an_unambiguous_index(self):
        resolve = build_slot_k_resolver(
            _state(
                [_profile(3, "0.020000", "0.4", extruder=0), _profile(4, "0.018000", "0.4", extruder=1)],
                ams_extruder_map={"0": 1},
            )
        )

        assert resolve(4, 2, 0) == pytest.approx(0.018)


class TestTheExternalHolder:
    def test_each_side_reads_its_own_hotend(self):
        """Ext-L (tray 0) feeds extruder 1, Ext-R (tray 1) extruder 0."""
        resolve = build_slot_k_resolver(
            _state(
                [_profile(3, "0.020000", "0.4", extruder=0), _profile(3, "0.018000", "0.4", extruder=1)],
                ams_extruder_map={"0": 1},
            )
        )

        assert resolve(3, 255, 0) == pytest.approx(0.018)
        assert resolve(3, 255, 1) == pytest.approx(0.020)

    def test_a_single_nozzle_printer_resolves_its_external_spool(self):
        resolve = build_slot_k_resolver(_state([_profile(3, "0.020000", "0.4")]))

        assert resolve(3, 255, 0) == pytest.approx(0.020)
