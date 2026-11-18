"""What a slicer picked for one printer does not travel to another (upstream Bambuddy dc72a0dae, c23c073dd).

On a nozzle-rack model (H2C) the slicer's ``nozzle_mapping`` names rack POSITIONS it read off the one
printer the Virtual Printer is bound to, and the dispatcher sends a captured mapping as it is. Kept on
a job that prints elsewhere -- the auto-queue tier, a fallback when the target is offline, a job moved
to another queue -- it selected another hotend. The nozzle offset calibration the operator chose was
never read at all. A rename reached nothing until the VP was restarted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.api.routes.print_queue import _drop_rack_pick_on_move
from backend.app.services.virtual_printer import manager as vp_manager
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance


def _vp(**fields) -> VirtualPrinterInstance:
    instance = VirtualPrinterInstance.__new__(VirtualPrinterInstance)
    instance.name = fields.pop("name", "Bench VP")
    instance.mode = fields.pop("mode", "print_queue")
    instance.target_printer_id = fields.pop("target_printer_id", None)
    instance._bind = instance._ssdp = instance._ssdp_proxy = instance._ftp = instance._mqtt = None
    for key, value in fields.items():
        setattr(instance, key, value)
    return instance


class TestRackPositionsStayWithTheirPrinter:
    def test_kept_for_the_printer_the_vp_is_bound_to(self):
        assert _vp(target_printer_id=4).keeps_slicer_nozzle_mapping("H2C", 4) is True

    def test_dropped_when_the_job_lands_on_another_printer(self):
        assert _vp(target_printer_id=4).keeps_slicer_nozzle_mapping("H2C", 9) is False

    def test_dropped_for_a_vp_without_a_target(self):
        assert _vp().keeps_slicer_nozzle_mapping("H2C", 9) is False

    def test_dropped_on_the_tier_that_has_no_printer_yet(self):
        assert _vp(target_printer_id=4, mode="auto_queue").keeps_slicer_nozzle_mapping("H2C", None) is False

    @pytest.mark.parametrize("model", ["H2D", "X2D", "X1C", None])
    def test_extruder_indexes_of_other_models_travel(self, model):
        assert _vp().keeps_slicer_nozzle_mapping(model, 9) is True


class TestAMovedJobLeavesItsRackPickBehind:
    def _item(self, mapping="[16, -1]"):
        return SimpleNamespace(queue_id=1, nozzle_mapping=mapping)

    def test_a_move_to_another_h2c_clears_it(self):
        changes: dict = {}
        _drop_rack_pick_on_move(self._item(), changes, SimpleNamespace(id=2), "H2C")
        assert changes == {"nozzle_mapping": None}

    def test_the_same_queue_keeps_it(self):
        changes: dict = {}
        _drop_rack_pick_on_move(self._item(), changes, SimpleNamespace(id=1), "H2C")
        assert changes == {}

    def test_an_edit_that_sets_the_mapping_is_left_alone(self):
        changes = {"nozzle_mapping": "[17, -1]"}
        _drop_rack_pick_on_move(self._item(), changes, SimpleNamespace(id=2), "H2C")
        assert changes == {"nozzle_mapping": "[17, -1]"}

    def test_another_model_keeps_it(self):
        changes: dict = {}
        _drop_rack_pick_on_move(self._item("[0, 1]"), changes, SimpleNamespace(id=2), "H2D")
        assert changes == {}

    def test_no_move_no_change(self):
        changes: dict = {}
        _drop_rack_pick_on_move(self._item(), changes, None, None)
        assert changes == {}


class TestNozzleOffsetCalibration:
    @pytest.mark.parametrize(("wire", "mode"), [(0, "off"), (1, "on"), (2, "auto"), (False, "off"), (True, "on")])
    def test_the_slicers_choice_is_taken(self, wire, mode):
        assert vp_manager._nozzle_offset_mode({"nozzle_offset_cali": wire}, {"nozzle_offset_cali": "on"}) == mode

    def test_the_saved_print_options_fill_in_when_the_slicer_is_silent(self):
        assert vp_manager._nozzle_offset_mode({"timelapse": True}, {"nozzle_offset_cali": "auto"}) == "auto"
        assert vp_manager._nozzle_offset_mode(None, {"nozzle_offset_cali": "off"}) == "off"

    def test_nobody_chose_is_what_the_column_defaulted_to(self):
        assert vp_manager._nozzle_offset_mode(None, None) == "on"
        assert vp_manager._nozzle_offset_mode({"nozzle_offset_cali": 7}, None) == "on"


class TestRenameOnTheFly:
    def test_the_running_services_answer_with_the_new_name(self):
        bind, ssdp, relay, ftp = (SimpleNamespace(name="Old"), SimpleNamespace(name="Old"), None, None)
        relay = SimpleNamespace(proxy_name="Old")
        ftp = SimpleNamespace(vp_name="Old")
        instance = _vp(name="Old")
        instance._bind, instance._ssdp, instance._ssdp_proxy, instance._ftp = bind, ssdp, relay, ftp

        instance.rename("Print room")

        assert (instance.name, bind.name, ssdp.name, relay.proxy_name, ftp.vp_name) == ("Print room",) * 5

    def test_a_proxy_keeps_its_suffix_in_discovery(self):
        ssdp = SimpleNamespace(name="Old (Proxy)")
        instance = _vp(name="Old", mode="proxy")
        instance._ssdp = ssdp

        instance.rename("Print room")

        assert ssdp.name == "Print room (Proxy)"

    def test_an_empty_name_changes_nothing(self):
        instance = _vp(name="Old")
        instance.rename("")
        assert instance.name == "Old"
