"""H2 / X2D: the active tray is read against the same frame's nozzle data, and an unresolved reading is
not journalled (upstream 3ddd324f8, e3824097f).

A dual-nozzle printer reports ``ams.tray_now`` as a bare slot 0-3; which AMS unit it belongs to is told by
the active nozzle's ``snow`` (``device.extruder.info``). Two defects in how the two were put together:

- the frame's AMS block was read BEFORE the same frame's nozzle data, so a frame carrying the new slot
  together with the new snow (an AMS Backup switch from tray 1 to tray 6) was resolved against the old
  snow: the tray stayed on 1, the backup reel never entered the print's chronology, and its layers were
  charged to tray 1;
- a slot nothing could resolve was written down as a tray of AMS 0 ("may be incorrect") and journalled as
  a ``tray_change`` with that tray's spool frozen. Such a phantom is a "return to the source" for the
  stale-refill proof, and closes a runout tail as a false ``feed_return``.

Read from the code and upstream's tests only: no capture of an H2 backup switch exists yet. Where this
module's assumption about frame contents could be wrong, the behaviour stays what it was (a snow that
disagrees and did not come in this frame still wins, exactly as before).

What must not move: the chronology of a print whose nozzle data arrive before its tray reports -- every
existing sequence -- is pinned below, entry for entry.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient

UNLOADED = 0xFFFF
NOT_YET = 0xFF00FF  # the firmware's initial sentinel: says nothing


def snow(ams: int, slot: int) -> int:
    return ams << 8 | slot


def nozzles(right=None, left=None, *, active: int | None = None) -> dict:
    extruder: dict = {}
    if right is not None or left is not None:
        extruder["info"] = [
            {"id": 0, "snow": NOT_YET if right is None else right},
            {"id": 1, "snow": NOT_YET if left is None else left},
        ]
    if active is not None:
        extruder["state"] = 0x0002 | (active << 4)
    return {"extruder": extruder}


def frame(*, tray_now=None, device=None, layer=None, mapping=None) -> dict:
    body: dict = {}
    if device is not None:
        body["device"] = device
    if tray_now is not None:
        body["ams"] = {"tray_now": str(tray_now)}
    if layer is not None:
        body["layer_num"] = layer
    if mapping is not None:
        body["mapping"] = mapping
    return {"print": body}


class Printer:
    """A client with a print running and its journal open; records what the journal is told."""

    def __init__(self, *, units: dict[str, int], model: str | None = None):
        self.client = BambuMQTTClient(ip_address="127.0.0.1", serial_number="H2-FRAME", access_code="x", model=model)
        self.facts: list[tuple[str, int, int]] = []
        self.client.on_tray_change = lambda tray, layer: self.facts.append(("tray_change", tray, layer))
        self.client.on_feed_event = lambda kind, tray, layer, _gap: self.facts.append((kind, tray, layer))
        if units:
            # Dual-nozzle detection and the wiring, as a pushall gives them.
            self.client._process_message(
                {
                    "print": {
                        "device": nozzles(UNLOADED, UNLOADED, active=0),
                        "ams": {
                            "ams": [
                                {
                                    "id": int(u),
                                    "info": f"2{e}03",
                                    "tray": [{"id": i, "tray_type": "PLA"} for i in range(4)],
                                }
                                for u, e in units.items()
                            ],
                            "tray_now": "255",
                        },
                    }
                }
            )
            assert self.client.state.ams_extruder_map == units

    def printing_from(self, tray: int, *, layer: int = 0):
        client = self.client
        client._was_running = True
        client._completion_triggered = False
        client._journal_held = False
        client.state.state = "RUNNING"
        client.state.tray_now = tray
        client.state.last_loaded_tray = tray
        client.state.layer_num = layer
        client.state.tray_change_log = [(tray, 0)]
        self.facts.clear()
        return self

    def send(self, **kwargs):
        self.client._process_message(frame(**kwargs))
        return self

    @property
    def tray_now(self) -> int:
        return self.client.state.tray_now

    @property
    def log(self) -> list:
        return list(self.client.state.tray_change_log)


TWO_AMS_ON_THE_RIGHT_NOZZLE = {"0": 0, "1": 0}
ONE_AMS_PER_NOZZLE = {"0": 0, "1": 1}


# ── the same frame's nozzle data ────────────────────────────────────────────


class TestTheFramesOwnNozzleDataIsReadFirst:
    def test_a_backup_switch_carried_with_its_snow_enters_the_chronology(self):
        """Was tray 1 and no entry: tray 6 fed the rest of the print and was charged nothing."""
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE)
        printer.send(device=nozzles(right=snow(0, 1)), tray_now=1).printing_from(1)

        printer.send(device=nozzles(right=snow(1, 2)), tray_now=2, layer=40)

        assert printer.tray_now == 6
        assert printer.log == [(1, 0), (6, 40)]
        assert printer.facts == [("tray_change", 6, 40)]

    def test_a_nozzle_switch_carried_with_its_tray_reads_the_new_nozzle(self):
        """Was tray 0: the right nozzle's old snow answered for a slot the left nozzle reported."""
        printer = Printer(units=ONE_AMS_PER_NOZZLE)
        printer.send(device=nozzles(right=snow(0, 0), left=snow(1, 0)), tray_now=0).printing_from(0)

        printer.send(device=nozzles(right=snow(0, 0), left=snow(1, 0), active=1), tray_now=0, layer=12)

        assert printer.tray_now == 4
        assert printer.facts == [("tray_change", 4, 12)]

    def test_malformed_nozzle_values_do_not_cost_the_frame_its_ams_block(self):
        printer = Printer(units=ONE_AMS_PER_NOZZLE)
        printer.send(device=nozzles(right=snow(0, 1)), tray_now=1).printing_from(1)
        broken = {"extruder": {"state": "x", "info": [{"id": 0, "snow": "x"}, {"id": "y", "snow": 3}, None]}}

        printer.client._process_message({"print": {"device": broken, "ams": {"tray_now": "255"}}})

        assert printer.tray_now == 255
        assert printer.client.state.h2d_extruder_snow.get(0) == 1  # the unreadable snow changed nothing


# ── a reading nothing resolves ──────────────────────────────────────────────


class TestAnUnresolvedSlotIsNotJournalled:
    def test_a_bare_slot_with_two_candidate_units_writes_no_tray_change(self):
        """Was ("tray_change", 3, 55): AMS 0 slot 4, with that tray's spool frozen beside it."""
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE).printing_from(5, layer=55)
        printer.client.state.tray_now = 255  # unloaded for the change

        printer.send(tray_now=3, layer=55)

        assert printer.facts == []
        assert printer.log == [(5, 0)]
        assert printer.client.state.last_loaded_tray == 5

    def test_the_live_mapping_names_the_unit_while_the_print_runs(self):
        """Was tray 3 (AMS 0): the print's own mapping says the slot is AMS 1's."""
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE).printing_from(5, layer=55)
        printer.client.state.tray_now = 255
        printer.client.state.raw_data["mapping"] = [snow(1, 3), 65535]

        printer.send(tray_now=3, layer=55)

        assert printer.tray_now == 7
        assert printer.facts == [("tray_change", 7, 55)]

    def test_a_mapping_with_two_candidates_decides_nothing(self):
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE).printing_from(5, layer=55)
        printer.client.state.tray_now = 255
        printer.client.state.raw_data["mapping"] = [snow(0, 3), snow(1, 3)]

        printer.send(tray_now=3, layer=55)

        assert printer.facts == []

    def test_a_mapping_entry_of_the_other_nozzles_unit_is_no_candidate(self):
        printer = Printer(units={"0": 0, "1": 0, "2": 1}).printing_from(5, layer=55)
        printer.client.state.tray_now = 255
        printer.client.state.raw_data["mapping"] = [snow(2, 3), snow(1, 3)]

        printer.send(tray_now=3, layer=55)

        assert printer.tray_now == 7
        assert printer.facts == [("tray_change", 7, 55)]

    def test_an_idle_printer_never_asks_the_last_prints_mapping(self):
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE)
        printer.client.state.raw_data["mapping"] = [snow(1, 3)]

        printer.send(tray_now=3)

        assert printer.tray_now == 3  # as before: nothing is printing, nothing is journalled
        assert printer.facts == []

    def test_a_slot_on_a_nozzle_with_no_unit_writes_no_tray_change(self):
        """Was ("tray_change", 2, 30)."""
        printer = Printer(units={"0": 1, "1": 1}).printing_from(254, layer=30)

        printer.send(tray_now=2, layer=30)

        assert printer.facts == []
        assert printer.client.state.last_loaded_tray == 254

    def test_an_open_runout_tail_is_not_closed_by_an_unresolved_slot(self):
        """Was a ``feed_return`` to tray 1: the phantom "AMS 0 slot 2" equalled the tail's origin."""
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE).printing_from(1, layer=70)
        printer.client._tail_origin = 1
        printer.client._tail_open_layer = 60
        printer.client.state.tray_now = 254

        printer.send(tray_now=1, layer=70)  # in truth AMS 1 slot 2 (tray 5) took over; snow not in yet

        assert printer.facts == []
        assert printer.client._tail_origin == 1

    def test_a_snow_of_an_earlier_frame_that_disagrees_writes_no_tray_change(self):
        """Was ("tray_change", 1, 80) -- a return to the source nobody made.

        The right nozzle's snow still says tray 1 from before its unload frame was lost; the print has been
        on tray 6 since. A report of slot 3 fits neither: the kept tray stands and nothing is journalled."""
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE)
        printer.send(device=nozzles(right=snow(0, 1)))
        printer.printing_from(6, layer=80)

        printer.send(tray_now=3, layer=80)

        assert printer.tray_now == 6
        assert printer.facts == []
        assert printer.log == [(6, 0)]

    def test_a_snow_of_an_earlier_frame_that_agrees_still_resolves(self):
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE)
        printer.send(device=nozzles(right=snow(1, 2)))
        printer.printing_from(1, layer=40)

        printer.send(tray_now=2, layer=41)

        assert printer.tray_now == 6
        assert printer.facts == [("tray_change", 6, 41)]


# ── what must not move ──────────────────────────────────────────────────────


class TestTheChronologyOfANormalPrintIsUnchanged:
    """GUARD 1: recorded on the code before this change, entry for entry."""

    def test_a_single_nozzle_multi_colour_print(self):
        """P1S, one AMS: this branch of the parser is not touched at all."""
        printer = Printer(units={}, model="P1S").printing_from(0)
        for layer, tray in [(1, 255), (1, 1), (2, 255), (2, 2), (3, 255), (3, 0), (4, 255), (4, 3), (9, 255), (9, 0)]:
            printer.send(tray_now=tray, layer=layer)

        assert printer.log == [(0, 0), (1, 1), (2, 2), (0, 3), (3, 4), (0, 9)]
        assert printer.facts == [
            ("tray_change", 1, 1),
            ("tray_change", 2, 2),
            ("tray_change", 0, 3),
            ("tray_change", 3, 4),
            ("tray_change", 0, 9),
        ]

    def test_a_dual_nozzle_multi_colour_print_whose_nozzle_data_come_first(self):
        """X2D, one AMS per nozzle: each change is an unload, the nozzle's snow, then the tray report."""
        printer = Printer(units=ONE_AMS_PER_NOZZLE)
        printer.send(device=nozzles(right=snow(0, 0), left=snow(1, 1)), tray_now=0).printing_from(0)
        steps = [
            # (layer, active nozzle, right snow, left snow, slot reported)
            (1, 1, snow(0, 0), snow(1, 1), 1),
            (2, 0, snow(0, 0), snow(1, 1), 0),
            (3, 0, snow(0, 2), snow(1, 1), 2),
            (4, 1, snow(0, 2), snow(1, 3), 3),
            (5, 0, snow(0, 2), snow(1, 3), 2),
        ]
        for layer, active, right, left, slot in steps:
            printer.send(device=nozzles(right=right, left=left, active=active), layer=layer)
            printer.send(tray_now=slot, layer=layer)

        assert printer.log == [(0, 0), (5, 1), (0, 2), (2, 3), (7, 4), (2, 5)]
        assert printer.facts == [
            ("tray_change", 5, 1),
            ("tray_change", 0, 2),
            ("tray_change", 2, 3),
            ("tray_change", 7, 4),
            ("tray_change", 2, 5),
        ]

    def test_full_frames_that_repeat_what_is_already_known(self):
        """H2D, two AMS on one nozzle: status frames carrying the nozzle data and the tray together,
        the snow the one already stored. A backup switch through an unload, as the firmware reports it."""
        printer = Printer(units=TWO_AMS_ON_THE_RIGHT_NOZZLE)
        printer.send(device=nozzles(right=snow(0, 1)), tray_now=1).printing_from(1)
        printer.send(device=nozzles(right=snow(0, 1), active=0), tray_now=1, layer=10)
        printer.send(device=nozzles(right=UNLOADED, active=0), tray_now=255, layer=30)
        printer.send(device=nozzles(right=snow(1, 2), active=0), layer=30)
        printer.send(tray_now=2, layer=30)
        printer.send(device=nozzles(right=snow(1, 2), active=0), tray_now=2, layer=31)

        assert printer.log == [(1, 0), (6, 30)]
        assert printer.facts == [("tray_change", 6, 30)]

    @pytest.mark.parametrize("tray", [0, 5, 128, 254])
    def test_a_tray_already_global_is_taken_as_reported(self, tray):
        printer = Printer(units=ONE_AMS_PER_NOZZLE).printing_from(1)

        printer.send(tray_now=tray if tray > 3 else 255, layer=3)

        assert printer.tray_now == (tray if tray > 3 else 255)
