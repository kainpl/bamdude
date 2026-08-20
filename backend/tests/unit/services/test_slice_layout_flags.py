"""Auto-arrange and auto-orient reach the sidecar, and only when asked for.

Ported from upstream #2548. Both are per-slice checkboxes, off by default,
forwarded as the sidecar's ``arrange`` / ``orient`` form fields.

⚠️ **An unticked box is sent by OMISSION.** The sidecar branches on
``settings.arrange !== undefined``, and multipart fields arrive as strings —
where ``"false"`` is truthy in JavaScript. Sending ``"false"`` would turn the
flag on for every slice.

⚠️ **Arrange unions with the automatic cross-nozzle-class decision** rather than
replacing it. That one is a correctness measure — without it the source's
coordinate layout lands in the target's dead zone — so an unticked box must not
switch it off.
"""

from __future__ import annotations

import inspect

import pytest

from backend.app.schemas.slicer import SliceRequest
from backend.app.services.slicer_api import SlicerApiService, _add_layout_flags


class TestTheFormFields:
    def test_neither_flag_is_sent_when_both_are_off(self):
        """⚠️ The whole point: an off flag is an ABSENT field, not "false"."""
        data: dict[str, str] = {}

        _add_layout_flags(data, arrange=False, orient=False)

        assert data == {}

    def test_a_default_slice_is_byte_identical_to_before_the_flags_existed(self):
        data = {"plate": "1", "exportType": "3mf"}

        _add_layout_flags(data, arrange=False, orient=False)

        assert data == {"plate": "1", "exportType": "3mf"}

    def test_arrange_is_sent_when_on(self):
        data: dict[str, str] = {}

        _add_layout_flags(data, arrange=True, orient=False)

        assert data == {"arrange": "true"}

    def test_orient_is_sent_when_on(self):
        data: dict[str, str] = {}

        _add_layout_flags(data, arrange=False, orient=True)

        assert data == {"orient": "true"}

    def test_both_together(self):
        data: dict[str, str] = {}

        _add_layout_flags(data, arrange=True, orient=True)

        assert data == {"arrange": "true", "orient": "true"}

    @pytest.mark.parametrize("field", ["arrange", "orient"])
    def test_no_flag_is_ever_sent_as_the_string_false(self, field):
        data: dict[str, str] = {}

        _add_layout_flags(data, arrange=False, orient=False)

        assert data.get(field) != "false"


class TestBothSlicePathsAcceptThem:
    """⚠️ They act on the loaded GEOMETRY, not on the print config, so a user's
    choice has to survive the embedded-settings route and the crash fallback —
    both of which go through ``slice_without_profiles``."""

    @pytest.mark.parametrize("method", ["slice_with_profiles", "slice_without_profiles"])
    def test_the_signature_carries_both(self, method):
        parameters = inspect.signature(getattr(SlicerApiService, method)).parameters

        assert "arrange" in parameters
        assert "orient" in parameters

    @pytest.mark.parametrize("method", ["slice_with_profiles", "slice_without_profiles"])
    def test_both_default_to_off(self, method):
        parameters = inspect.signature(getattr(SlicerApiService, method)).parameters

        assert parameters["arrange"].default is False
        assert parameters["orient"].default is False

    @pytest.mark.parametrize("method", ["slice_with_profiles", "slice_without_profiles"])
    def test_neither_builds_the_fields_by_hand(self, method):
        source = inspect.getsource(getattr(SlicerApiService, method))

        assert "_add_layout_flags(data, arrange=arrange, orient=orient)" in source
        assert 'data["arrange"]' not in source


class TestTheRequestSchema:
    def test_both_default_to_off(self):
        request = SliceRequest(printer_preset_id=1, process_preset_id=2, filament_preset_id=3)

        assert request.auto_arrange is False
        assert request.auto_orient is False

    def test_both_can_be_asked_for(self):
        request = SliceRequest(
            printer_preset_id=1, process_preset_id=2, filament_preset_id=3, auto_arrange=True, auto_orient=True
        )

        assert request.auto_arrange is True
        assert request.auto_orient is True


class TestTheRouteWiring:
    @staticmethod
    def _source() -> str:
        from backend.app.api.routes import library

        return inspect.getsource(library)

    def test_arrange_unions_with_the_cross_class_decision(self):
        """⚠️ Not a replacement: the cross-class arrange is a correctness
        measure and an unticked box must not switch it off."""
        assert "arrange = cross_class_arrange or request.auto_arrange" in self._source()

    def test_the_slice_all_loop_is_keyed_on_the_flag_not_the_crossing(self):
        """The project-wide collapse belongs to --arrange, so a user who ticks
        the box on a same-class slice-all needs the same per-plate treatment."""
        assert "use_cross_class_slice_all = arrange and request.plate == 0" in self._source()

    def test_a_single_call_slice_all_does_not_forward_arrange(self):
        """⚠️ Arrange is project-wide, so one ``--slice 0 --arrange`` call
        consolidates every plate onto one bed. The paths that cannot loop —
        embedded settings, and the crash fallback — must not send it."""
        source = self._source()
        assert "arrange_single_call = arrange and not (request.plate == 0 and request.export_3mf)" in source
        assert source.count("arrange=arrange_single_call,") == 2

    def test_orient_reaches_every_path(self):
        """Unlike arrange it has no plate-consolidation hazard, so it is
        forwarded verbatim wherever a slice is issued."""
        assert self._source().count("orient=request.auto_orient,") == 4
