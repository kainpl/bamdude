"""Printing on H3C / P3C — Bambu Studio's dispatch to a direct-connected rack (N01 stage B).

Frames are stage A's (``test_h3c_p3c_telemetry``): the H3C one trimmed from
upstream's live capture (every hotend ``stat`` 168 — filament at its inlet —,
the TPU285 hotend of dock 7 with its own ``snow`` 0xF800, input 248), the P3C
one synthetic. What Studio sends is quoted from ``bs-dispatch-research-B.md``
(BS v02.09.01.70 / v02.09.02.62).
"""

import copy
import json
from dataclasses import replace

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, resolve_external_spools_in_mapping
from backend.app.services.filament_requirements import PrintRequirements, SourceIdentity
from backend.app.services.filament_routing import (
    RoutingPolicy,
    calibration_hotend_refusal,
    resolve_filament_routing,
)
from backend.app.services.printer_feed_snapshot import snapshot_from_state
from backend.app.utils.direct_rack import (
    BLOCKED_BY_EXTERNAL,
    MANUAL_FEED,
    RACK_TYPE_A,
    RACK_TYPE_B,
    RackHotend,
    bound_slot,
    build_rack_state,
    feed_block,
    filament_setting_ids,
    hotend_matches,
    inlet_loaded,
    project_file_entry,
)
from backend.app.utils.tray_ids import TrayLayout, names_loaded_source
from backend.tests.unit.test_h3c_p3c_telemetry import H3C_FRAME, P3C_FRAME

H3C_LAYOUT = TrayLayout.build([{"id": 32, "info": "240842026"}], [{"id": v} for v in range(247, 256)], model="H3C")
P3C_LAYOUT = TrayLayout.build([{"id": 33, "info": "7"}], [{"id": v} for v in range(249, 256)], model="P3C")


def _client(frame, model, serial="H3CB0001"):
    client = BambuMQTTClient(ip_address="192.0.2.10", serial_number=serial, access_code="x", model=model)
    client._process_message({"print": copy.deepcopy(frame)})
    client.state.connected = True
    client._client = _Recorder()
    return client


class _Recorder:
    """A paho stand-in that keeps what was published."""

    def __init__(self):
        self.sent: list[dict] = []

    def publish(self, topic, payload, qos=0):
        self.sent.append(json.loads(payload))


@pytest.fixture
def h3c():
    return _client(H3C_FRAME, "H3C")


@pytest.fixture
def p3c():
    return _client(P3C_FRAME, "P3C", serial="P3CB0001")


def requirements(*slots, model="N10"):
    return PrintRequirements(
        "ok",
        source_identity=SourceIdentity("synthetic", 1, 1),
        resolved_plate_id=1,
        model=model,
        used_filaments=tuple(
            {"slot_id": i + 1, "type": "PLA", "color": "#DE4343", "used_grams": 1, "tray_info_idx": None, **s}
            for i, s in enumerate(slots)
        ),
    )


# --- the rack's hotends (utils/direct_rack) --------------------------------------


def test_the_inlet_flag_is_stat_bit_5_or_bits_6_to_8():
    assert inlet_loaded(0b100000) is True
    assert inlet_loaded(0b010000000) is True  # bits 6-8 = 2
    assert inlet_loaded(0b1000) is False
    assert inlet_loaded(168) is True  # the H3C capture
    assert inlet_loaded(None) is False


def test_h3c_hotends_by_dock_the_mounted_one_included(h3c):
    rack = h3c.rack_state()
    assert rack.known and len(rack.hotends) == 9
    mounted = rack.hotend(6)
    assert mounted.exists and mounted.diameter == 0.4 and mounted.flow == "standard"  # HS01: H(otend) S(tandard)
    tpu = rack.hotend(7)
    assert tpu.tpu285 and tpu.slot_now == 248
    assert rack.hotend(8).diameter == 0.6
    # The H3C capture carries no holder: the dock bits are unknown, never "uncalibrated".
    assert rack.any_dock_calibrated is None
    assert rack.rack_type == RACK_TYPE_A  # told from the AMS 3H


def test_the_exist_mask_by_full_id_and_a_detached_tool():
    entries = [{"id": 16, "addr_id": 0, "type": "HS00", "diameter": 0.4}, {"id": 17, "addr_id": 1, "type": "HS00"}]
    rack = build_rack_state(entries, H3C_LAYOUT, exist_mask=1 << 16, hc_mask=0b11)
    assert rack.hotend(0).exists and not rack.hotend(0).detached
    assert not rack.hotend(1).exists and rack.hotend(1).detached  # hc set, not in the holder
    assert not rack.hotend(2).exists and not rack.hotend(2).detached  # no entry: an empty dock


def test_dock_calibration_bits_of_holder_info2():
    rack = build_rack_state([{"id": 16, "addr_id": 0, "type": "HS00"}], H3C_LAYOUT, holder_info2=(1 << 5) | 0b11)
    assert rack.hotend(0).dock_calibrated is True
    assert rack.hotend(1).dock_calibrated is False
    assert rack.any_dock_calibrated is True


@pytest.mark.parametrize(
    ("rack_type", "expected"),
    [(RACK_TYPE_A, 248), (RACK_TYPE_B, 28 + 7)],
    ids=["H3C-TPU285-takes-its-input", "BS62-P3C-TPU285-keeps-its-slot"],
)
def test_the_tpu285_rule_keys_on_rack_type_a(rack_type, expected):
    hotend = RackHotend(addr=7, exists=True, flow="tpu285")
    trays = {28 + k for k in range(9)}
    assert bound_slot(hotend, H3C_LAYOUT, trays, set(range(247, 256)), rack_type) == expected


def test_the_bound_slot_snow_first_then_the_rack_slot_then_the_input():
    trays, externals = {28 + k for k in range(9)}, set(range(247, 256))
    assert bound_slot(RackHotend(addr=2, exists=True, slot_now=31), H3C_LAYOUT, trays, externals, 1) == 31
    assert bound_slot(RackHotend(addr=2, exists=True), H3C_LAYOUT, trays, externals, 1) == 30
    assert bound_slot(RackHotend(addr=2, exists=True), H3C_LAYOUT, set(), externals, 1) == 253  # 255 - 2


def test_a_snow_on_a_bind_ams_tray_counts_only_with_filament_at_the_inlet():
    layout = TrayLayout.build([{"id": 32, "info": "6"}, {"id": 0, "info": "1"}], [], model="H3C")
    trays = {28 + k for k in range(9)} | {0, 1, 2, 3}
    loaded = RackHotend(addr=4, exists=True, slot_now=1, inlet_loaded=True, bind_ams=(0,))
    empty_inlet = replace(loaded, inlet_loaded=False)
    assert bound_slot(loaded, layout, trays, set(), 1) == 1
    assert bound_slot(empty_inlet, layout, trays, set(), 1) == 32  # its rack slot instead


def test_feed_block_is_studios_query_slot_feed_block():
    ext_loaded = RackHotend(addr=7, exists=True, slot_now=248, inlet_loaded=True)
    assert feed_block(ext_loaded, 248, H3C_LAYOUT) is None  # the occupant
    assert feed_block(ext_loaded, 35, H3C_LAYOUT) == BLOCKED_BY_EXTERNAL
    assert feed_block(RackHotend(addr=3, exists=True), 31, H3C_LAYOUT) is None  # AMS 3H feeds itself
    assert feed_block(RackHotend(addr=3, exists=True), 252, H3C_LAYOUT) == MANUAL_FEED  # an unloaded input
    assert feed_block(RackHotend(addr=3, exists=True), 37 + 3, P3C_LAYOUT) == MANUAL_FEED  # AMS 3P never feeds


def test_the_hotend_spec_match():
    hotend = RackHotend(addr=0, exists=True, diameter=0.4, flow="high_flow")
    assert hotend_matches(hotend, 0.4, "High Flow")
    assert hotend_matches(hotend, 0.4, "Hybrid")  # several flows: any
    assert hotend_matches(hotend, None, "")  # nothing asked
    assert not hotend_matches(hotend, 0.6, "High Flow")
    assert not hotend_matches(hotend, 0.4, "Standard")
    assert not hotend_matches(hotend, 0.4, "Something New")  # an unknown flow still mismatches (BS)


# --- the project_file wire (§6) ---------------------------------------------------


@pytest.mark.parametrize(
    ("layout", "mapping", "flat", "detail"),
    [
        (
            H3C_LAYOUT,
            [30, 33, 248, -1],
            [30, 33, 248, -1],
            [
                {"ams_id": 32, "slot_id": 2},
                {"ams_id": 32, "slot_id": 5},
                {"ams_id": 248, "slot_id": 0},
                {"ams_id": 255, "slot_id": 255},
            ],
        ),
        (
            P3C_LAYOUT,
            [39, 42, 249, -1],
            [39, 42, 249, -1],
            [
                {"ams_id": 33, "slot_id": 2},
                {"ams_id": 33, "slot_id": 5},
                {"ams_id": 249, "slot_id": 0},
                {"ams_id": 255, "slot_id": 255},
            ],
        ),
        (H3C_LAYOUT, [254, 255], [-1, -1], [{"ams_id": 254, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}]),
    ],
    ids=["h3c", "p3c", "254-255-as-minus-one"],
)
def test_project_file_entries_are_studios(layout, mapping, flat, detail):
    entries = [project_file_entry(v, layout) for v in mapping]
    assert [e[0] for e in entries] == flat
    assert [e[1] for e in entries] == detail


def test_a_calibration_print_sends_its_tray_raw():
    assert project_file_entry(255, H3C_LAYOUT, raw_externals=True) == (255, {"ams_id": 255, "slot_id": 0})


def test_start_print_on_h3c(h3c):
    assert h3c.start_print("job.3mf", ams_mapping=[30, 33, 248, -1], nozzle_offset_cali="auto", use_ams=True)
    cmd = h3c._client.sent[-1]["print"]
    assert cmd["ams_mapping"] == [30, 33, 248, -1]
    assert cmd["ams_mapping2"][0] == {"ams_id": 32, "slot_id": 2}
    assert "nozzle_mapping" not in cmd
    assert cmd["nozzle_offset_cali"] == 2  # N10 supports the automatic mode
    assert cmd["use_ams"] is True


@pytest.mark.parametrize(("mapping", "use_ams"), [([248], True), ([254], False), ([255, 254], False), ([30], True)])
def test_use_ams_is_false_only_when_every_tray_is_254_or_255(h3c, mapping, use_ams):
    assert h3c.start_print("job.3mf", ams_mapping=mapping, use_ams=not use_ams)
    assert h3c._client.sent[-1]["print"]["use_ams"] is use_ams


def test_a_captured_nozzle_mapping_never_goes_to_a_direct_rack(h3c):
    h3c._is_dual_nozzle = True  # even if something marked it dual
    h3c.start_print("job.3mf", ams_mapping=[30], nozzle_mapping=json.dumps([16]))
    assert "nozzle_mapping" not in h3c._client.sent[-1]["print"]


def test_start_print_calibration_keeps_255_raw(h3c):
    h3c.start_print("cali.3mf", ams_mapping=[255], calibration=True)
    cmd = h3c._client.sent[-1]["print"]
    assert cmd["ams_mapping"] == [255]
    assert cmd["ams_mapping2"] == [{"ams_id": 255, "slot_id": 0}]


# --- the slot setting (§7) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("layout", "slot", "wire"),
    [
        (H3C_LAYOUT, (32, 5), (32, 5, 5)),
        (P3C_LAYOUT, (33, 2), (33, 2, 2)),
        (H3C_LAYOUT, (255, 0), (254, 254, 0)),  # input 254 (PrintTool 2)
        (H3C_LAYOUT, (255, 1), (255, 254, 0)),  # input 255 (PrintTool 1)
        (H3C_LAYOUT, (255, 3), (248, 0, 0)),  # input 248 (PrintTool 8)
    ],
)
def test_filament_setting_ids_are_studios(layout, slot, wire):
    assert filament_setting_ids(*slot, layout) == wire


def test_filament_setting_ids_leave_other_printers_alone():
    assert filament_setting_ids(0, 1, TrayLayout()) is None
    assert filament_setting_ids(255, 0, TrayLayout()) is None


def test_ams_filament_setting_on_a_rack_slot(h3c):
    assert h3c.ams_set_filament_setting(32, 5, "GFL99", "PLA", "", "DE4343FF", 190, 230)
    cmd = h3c._client.sent[-1]["print"]
    assert (cmd["ams_id"], cmd["slot_id"], cmd["tray_id"]) == (32, 5, 5)


def test_reset_slot_on_a_rack_input(h3c):
    assert h3c.reset_ams_slot(255, 3)
    cmd = h3c._client.sent[-1]["print"]
    assert (cmd["ams_id"], cmd["slot_id"], cmd["tray_id"]) == (248, 0, 0)


def test_k_profile_and_rfid_stay_refused_on_a_rack_slot(h3c):
    assert h3c.extrusion_cali_sel(32, 5, cali_idx=1, filament_id="GFL99", nozzle_diameter="0.4") is False


def test_the_developer_mode_probe_resends_input_255_by_its_id(h3c):
    h3c.state.raw_data["vt_tray"] = [
        {"id": "248", "tray_type": "TPU", "tray_color": "000000FF"},
        {"id": "255", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
    ]
    h3c._probe_developer_mode()
    cmd = h3c._client.sent[-1]["print"]
    assert (cmd["ams_id"], cmd["tray_id"], cmd["tray_type"]) == (255, 254, "PLA")


# --- the feed snapshot (§5.1) ----------------------------------------------------------


def test_the_h3c_snapshot_names_each_sources_print_tool(h3c):
    snap = snapshot_from_state(1, "H3C", h3c.state)
    assert snap.direct_rack
    by_id = {s.id: s for s in snap.sources}
    assert {s.id for s in snap.sources if s.kind == "ams"} == {28 + k for k in range(9) if k != 7}
    assert by_id[34].rack_addr == 6 and by_id[34].nozzles == (0,)
    assert by_id[28].feed_block is None
    assert snap.nozzle_diameters == {0: (0.4, 0.6)}


def test_a_hotend_swap_moves_the_revision(h3c):
    before = snapshot_from_state(1, "H3C", h3c.state).revision
    h3c.state.nozzle_rack = [dict(n, diameter="0.6") if n.get("addr_id") == 3 else n for n in h3c.state.nozzle_rack]
    assert snapshot_from_state(1, "H3C", h3c.state).revision != before


def test_the_p3c_snapshot_keeps_its_ams_3p_slots_manual_feed(p3c):
    p3c.state.nozzle_rack = [{"id": 16 + a, "addr_id": a, "type": "HS00", "diameter": "0.4"} for a in range(7)]
    snap = snapshot_from_state(2, "P3C", p3c.state)
    assert {s.feed_block for s in snap.sources if s.kind == "ams"} == {MANUAL_FEED}


# --- the routing rule (§3, §5.3) --------------------------------------------------------


def _h3c_snapshot(h3c):
    return snapshot_from_state(1, "H3C", h3c.state)


def test_routing_on_an_h3c_plans_a_rack_slot(h3c):
    result = resolve_filament_routing(requirements({"nozzle_diameter": 0.4}), RoutingPolicy(), _h3c_snapshot(h3c))
    assert result.status == "compatible"
    tray = result.plan.assignments[1].id
    assert 28 <= tray <= 36 and result.plan.direct_rack


def test_two_filaments_may_share_one_tray(h3c):
    snap = _h3c_snapshot(h3c)
    only = replace(snap, sources=tuple(s for s in snap.sources if s.id == 28))
    result = resolve_filament_routing(requirements({}, {}), RoutingPolicy(), only)
    assert result.status == "compatible"
    assert result.plan.mapping == [28, 28]


def test_a_hotend_of_another_size_refuses(h3c):
    result = resolve_filament_routing(requirements({"nozzle_diameter": 0.2}), RoutingPolicy(), _h3c_snapshot(h3c))
    # "unknown", not "incompatible": the capture's slot 7 reports no type yet (an incomplete feed).
    assert result.status != "compatible" and result.reason == "rack_hotend_mismatch"


def test_a_moving_rack_waits(h3c):
    h3c.state.rack_holder_status = 2
    result = resolve_filament_routing(requirements({}), RoutingPolicy(), _h3c_snapshot(h3c))
    assert (result.status, result.reason) == ("unknown", "rack_busy")


def test_uncalibrated_docks_refuse_once_the_holder_says_so(h3c):
    h3c.state.rack_holder_info2 = 0b11  # type A, no dock calibrated
    result = resolve_filament_routing(requirements({}), RoutingPolicy(), _h3c_snapshot(h3c))
    assert result.reason == "rack_not_calibrated"


def test_a_dynamic_nozzle_map_slice_is_refused(h3c):
    req = replace(requirements({}), nozzle_constraints={"dynamic_nozzle_map": True})
    assert resolve_filament_routing(req, RoutingPolicy(), _h3c_snapshot(h3c)).reason == "dynamic_nozzle_map_unsupported"


#: P3C docks 0-6 docking-calibrated (``holder.info2`` bits 5 + addr), rack type B, setup done.
P3C_CALIBRATED = 0b101 | (0x7F << 5)


def test_a_p3c_with_only_manual_feed_names_the_print_tools(p3c):
    p3c.state.nozzle_rack = [{"id": 16 + a, "addr_id": a, "type": "HS00", "diameter": "0.4"} for a in range(7)]
    p3c.state.rack_holder_info2 = P3C_CALIBRATED
    result = resolve_filament_routing(
        requirements({"color": "#FFFFFF"}, model="N11"), RoutingPolicy(), snapshot_from_state(2, "P3C", p3c.state)
    )
    assert result.reason == "rack_manual_feed_only"
    assert result.params["print_tools"]


def test_a_pin_takes_a_manual_feed_slot_with_a_notice(p3c):
    p3c.state.nozzle_rack = [{"id": 16 + a, "addr_id": a, "type": "HS00", "diameter": "0.4"} for a in range(7)]
    p3c.state.rack_holder_info2 = P3C_CALIBRATED
    policy = RoutingPolicy(mode="pinned", physical_pins={1: {"source_id": 40}})
    result = resolve_filament_routing(
        requirements({"color": "#FFFFFF"}, model="N11"), policy, snapshot_from_state(2, "P3C", p3c.state)
    )
    assert result.status == "compatible" and result.plan.mapping == [40]
    assert [n["code"] for n in result.notices] == ["manual_feed"]
    assert result.notices[0]["print_tool"] == 4


def test_the_tpu285_input_is_blocked_for_other_trays_of_its_hotend(h3c):
    snap = _h3c_snapshot(h3c)
    assert snap.rack.hotend(7).slot_now == 248 and snap.rack.hotend(7).inlet_loaded


# --- calibration (§9) ----------------------------------------------------------------------


def test_a_calibration_asks_the_hotend_its_tray_leads_to(h3c):
    snap = _h3c_snapshot(h3c)
    assert calibration_hotend_refusal(snap, 34, 0.4, "standard") is None
    assert calibration_hotend_refusal(snap, 34, 0.6, "standard") == "rack_hotend_mismatch"
    assert calibration_hotend_refusal(snap, 34, 0.4, "high_flow") == "rack_hotend_mismatch"
    assert calibration_hotend_refusal(snap, 34, 0.4, "hybrid") == "rack_hotend_mismatch"  # one hotend, one flow
    assert (
        calibration_hotend_refusal(snap, 248, 0.4, "tpu_high_flow") == "rack_hotend_mismatch"
    )  # TPU285: no wizard flow


# --- 255 and the capture (§5.5, §8) ---------------------------------------------------------


def test_input_255_is_journalled_only_on_the_snow(h3c):
    frame = copy.deepcopy(H3C_FRAME)
    frame["device"]["extruder"]["info"][0]["snow"] = 0xFF00  # PrintTool 1's input
    h3c._process_message({"print": frame})
    assert h3c.state.tray_now == 255 and h3c.state.direct_rack_input_loaded
    assert h3c.tray_layout().is_journal_tray(255)
    assert names_loaded_source(255, h3c.state)
    frame["device"]["extruder"]["info"][0]["snow"] = 0xFFFF  # nothing
    h3c._process_message({"print": frame})
    assert h3c.state.tray_now == 255 and not h3c.state.direct_rack_input_loaded
    assert not h3c.tray_layout().is_journal_tray(255)


def test_a_bare_tray_now_255_does_not_set_the_flag(h3c):
    frame = {"command": "push_status", "ams": {"tray_now": "255"}}
    h3c._process_message({"print": frame})
    assert not h3c.state.direct_rack_input_loaded


def test_255_stays_nothing_elsewhere():
    class _State:
        direct_rack_input_loaded = False

    assert not names_loaded_source(255, _State())
    assert names_loaded_source(254, _State())


def test_the_capture_keeps_input_255_on_a_direct_rack():
    flat = resolve_external_spools_in_mapping(
        [-1, -1, 248],
        [{"ams_id": 255, "slot_id": 0}, {"ams_id": 254, "slot_id": 0}, {"ams_id": 248, "slot_id": 0}],
        False,
        H3C_LAYOUT,
    )
    assert flat == [255, 254, 248]


def test_the_capture_folds_255_to_254_on_a_single_nozzle_printer_as_before():
    flat = resolve_external_spools_in_mapping([-1], [{"ams_id": 255, "slot_id": 0}], False, TrayLayout())
    assert flat == [254]


def test_a_pin_on_an_uncalibrated_dock_is_refused(p3c):
    p3c.state.nozzle_rack = [{"id": 16 + a, "addr_id": a, "type": "HS00", "diameter": "0.4"} for a in range(7)]
    p3c.state.rack_holder_info2 = 0b101 | (1 << 5)  # only dock 0 calibrated
    policy = RoutingPolicy(mode="pinned", physical_pins={1: {"source_id": 40}})
    result = resolve_filament_routing(
        requirements({"color": "#FFFFFF"}, model="N11"), policy, snapshot_from_state(2, "P3C", p3c.state)
    )
    assert result.reason == "rack_not_calibrated"


def test_a_tpu285_hotends_own_rack_slot_is_offered_to_a_pin_only():
    """Studio's preload card (HVM:676-700): the rack slot of a hotend bound to its input."""
    from backend.app.utils.direct_rack import PRELOAD

    frame = copy.deepcopy(H3C_FRAME)
    frame["ams"]["ams"][0]["tray"][7] = {
        "id": "7",
        "state": 11,
        "tray_type": "PLA",
        "tray_color": "DE4343FF",
        "tray_info_idx": "GFL99",
        "remain": -1,
    }
    frame["ams"]["tray_exist_bits"] = hex(int(frame["ams"]["tray_exist_bits"], 16) | (1 << 35))[2:]  # slot 7 present
    client = _client(frame, "H3C", serial="H3CB0002")
    snap = snapshot_from_state(1, "H3C", client.state)
    preload = next(s for s in snap.sources if s.id == 35)
    assert (preload.rack_addr, preload.feed_block) == (7, PRELOAD)
    auto = resolve_filament_routing(requirements({}), RoutingPolicy(), snap)
    assert auto.plan.mapping != [35]
    pinned = resolve_filament_routing(
        requirements({}), RoutingPolicy(mode="pinned", physical_pins={1: {"source_id": 35}}), snap
    )
    assert pinned.status == "compatible" and pinned.plan.mapping == [35]
    assert pinned.notices == ()  # a preload card carries no warning in Studio


# --- acceptance r1 (B2-02 … B2-04) --------------------------------------------------------


def test_a_slice_without_per_filament_data_is_checked_against_the_project_diameter(h3c):
    """B2-02 / spec §5.2: no ``<filament nozzle_diameter>`` -> the project's diameter, flow any."""
    req = replace(requirements({}), nozzle_constraints={"nozzle_diameter": ["0.6"]})
    planned = resolve_filament_routing(req, RoutingPolicy(), _h3c_snapshot(h3c))
    assert planned.status == "compatible" and planned.plan.mapping == [36]  # dock 8 holds the 0.6
    h3c.state.nozzle_rack = [dict(n, diameter=0.4) if n.get("addr_id") == 8 else n for n in h3c.state.nozzle_rack]
    refused = resolve_filament_routing(req, RoutingPolicy(), _h3c_snapshot(h3c))
    assert refused.plan is None and refused.reason == "rack_hotend_mismatch"


def test_the_print_tool_numbers_reach_the_sentence():
    """B2-03: the numbers ride as a sentence of their own, absent when nobody knows them."""
    from backend.app.services.filament_intake import routing_detail

    with_numbers = routing_detail("rack_manual_feed_only", print_tools="4, 6")["message"]
    assert "PrintTool: 4, 6." in with_numbers
    assert "{" not in routing_detail("rack_manual_feed_only")["message"]


def test_a_slot_the_rack_does_not_have_is_not_sent(p3c):
    """B2-04: a P3C has no input 247 (PrintTool 9): neither the setting nor the reset goes out."""
    assert p3c.ams_set_filament_setting(255, 2, "GFL99", "PLA", "", "FFFFFFFF", 190, 230) is False
    assert p3c.reset_ams_slot(255, 2) is False
    assert p3c.ams_set_filament_setting(33, 7, "GFL99", "PLA", "", "FFFFFFFF", 190, 230) is False
    assert p3c._client.sent == []
    assert p3c.ams_set_filament_setting(33, 6, "GFL99", "PLA", "", "FFFFFFFF", 190, 230) is True
