"""Regression tests for D.3: ``_resolve_global_tray_id`` honours ``-1`` in
``slot_to_tray`` as "external spool".

Upstream Bambuddy #1276 / commit 6fe00adb. BambuStudio converts virtual
tray IDs (254 / 255) to ``-1`` in the flat ``ams_mapping`` array before
sending the print command. Treating ``-1`` as "unmapped, use position-
based default" credited external-spool prints to whatever Spoolman spool
was linked to AMS slot 0 (regression of #853).
"""

from __future__ import annotations

from backend.app.services.spoolman_tracking import _resolve_global_tray_id


class TestResolveGlobalTrayIdExternalSpool:
    def test_minus_one_with_external_spool_resolves_to_254(self):
        """Single-nozzle reports ``tray_now=254`` for external spool, so
        prefer 254 when both 254 and 255 exist in the tray map."""
        ams_trays = {0: {}, 1: {}, 254: {}}
        assert _resolve_global_tray_id(1, slot_to_tray=[-1], ams_trays=ams_trays) == 254

    def test_minus_one_prefers_254_over_255_when_both_present(self):
        """Dual-nozzle (H2D) has both 254 (deputy) and 255 (main). The
        helper picks 254 deterministically per the docstring."""
        ams_trays = {0: {}, 254: {}, 255: {}}
        assert _resolve_global_tray_id(1, slot_to_tray=[-1], ams_trays=ams_trays) == 254

    def test_minus_one_falls_back_to_255_when_only_main_external_present(self):
        ams_trays = {0: {}, 255: {}}
        assert _resolve_global_tray_id(1, slot_to_tray=[-1], ams_trays=ams_trays) == 255

    def test_positive_mapping_still_used_when_present(self):
        """Regression guard: positive (slot → tray) mapping wins over
        the new ``-1`` external-spool path."""
        ams_trays = {0: {}, 1: {}, 254: {}}
        assert _resolve_global_tray_id(1, slot_to_tray=[2], ams_trays=ams_trays) == 2

    def test_minus_one_without_external_in_ams_falls_through_to_position_default(self):
        """If ``-1`` is set but ``ams_trays`` doesn't actually have 254 or 255
        (e.g. printer reconnect mid-print where AMS state is stale), fall
        through to the position-based default rather than crashing."""
        ams_trays = {0: {}, 1: {}}
        # slot_id=1 → position-default picks sorted_tray_ids[0] = 0
        assert _resolve_global_tray_id(1, slot_to_tray=[-1], ams_trays=ams_trays) == 0

    def test_minus_one_without_ams_trays_uses_legacy_fallback(self):
        """No ``ams_trays`` supplied → legacy ``slot_id - 1`` fallback."""
        assert _resolve_global_tray_id(1, slot_to_tray=[-1]) == 0

    def test_position_default_unchanged_for_no_custom_mapping(self):
        """Regression: position-based default still picks sorted_tray_ids[slot_id - 1]."""
        ams_trays = {0: {}, 1: {}, 254: {}}
        assert _resolve_global_tray_id(3, slot_to_tray=None, ams_trays=ams_trays) == 254
