"""A new connection cannot inherit proof of the last connection's spools."""

from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState
from backend.app.services.printer_feed_snapshot import FeedTelemetry, snapshot_from_state


def test_partial_update_retains_fields_but_new_generation_does_not():
    state = PrinterState(connected=True, connection_generation=1)
    state.feed_telemetry.observe(
        {"print": {"ams": {"ams": []}, "vt_tray": {"id": 254, "tray_type": "PLA", "tray_color": "FF0000"}}}, "P1P"
    )
    first = snapshot_from_state(1, "C11", state)
    state.feed_telemetry.observe({"print": {"bed_temper": 60, "vt_tray": {"id": 254, "remain": 30}}}, "P1P")
    second = snapshot_from_state(1, "C11", state)
    assert second.marker == first.marker
    assert second.sources[0].material == "PLA"
    state.feed_telemetry = FeedTelemetry()
    state.connection_generation += 1
    state.feed_telemetry.observe({"print": {"bed_temper": 60, "vt_tray": {"id": 254, "remain": 30}}}, "P1P")
    third = snapshot_from_state(1, "C11", state)
    assert third.incomplete and not third.ams_known
    assert third.sources == ()


def test_explicit_empty_external_and_ams_units_clear_sources():
    state = PrinterState(connected=True)
    state.feed_telemetry.observe(
        {
            "print": {
                "ams": {"ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}]},
                "vt_tray": {"id": 254, "tray_type": "PLA"},
            }
        },
        "P1P",
    )
    assert len(snapshot_from_state(1, "P1P", state).sources) == 2
    state.feed_telemetry.observe({"print": {"ams": {"ams_exist_bits": "0"}, "vt_tray": []}}, "P1P")
    result = snapshot_from_state(1, "P1P", state)
    assert result.ams_known and result.external_known
    assert not result.ams_present and not result.sources


def test_real_mqtt_parser_records_top_level_ams_and_ignores_calibration_diameter():
    client = BambuMQTTClient("127.0.0.1", "SYNTHETIC", "00000000", model="P1P")
    client.state.connected = True
    client._process_message({"ams": {"ams": []}})
    client._process_message(
        {"print": {"command": "push_status", "vt_tray": {"id": 254, "tray_type": "PLA"}, "nozzle_diameter": "0.4"}}
    )
    first = client.get_feed_snapshot(1)
    client._process_message({"print": {"command": "extrusion_cali_get", "nozzle_diameter": "0.8"}})
    assert client.get_feed_snapshot(1).marker == first.marker
    assert first.ams_known and first.nozzle_diameters == {0: (0.4,)}


def test_h2c_rack_nozzles_and_fts_use_physical_capabilities():
    state = PrinterState(connected=True)
    state.feed_telemetry.observe(
        {
            "print": {
                "device": {
                    "fila_switch": {},
                    "nozzle": {
                        "info": [
                            {"id": 1, "diameter": "0.4"},
                            {"id": 16, "diameter": "0.6"},
                            {"id": 17, "diameter": "0.4"},
                        ]
                    },
                },
                "ams": {"ams": [{"id": 0, "info": "e00", "tray": [{"id": 0, "tray_type": "PLA"}]}]},
            }
        },
        "H2C",
    )
    snap = snapshot_from_state(1, "H2C", state)
    assert snap.fts and snap.sources[0].nozzles == (0, 1)
    assert snap.nozzle_diameters == {0: (0.4,), 1: (0.4, 0.6)}


def test_disconnected_unit_and_invalid_slot_cannot_survive_partial_update():
    state = PrinterState(connected=True)
    state.feed_telemetry.observe(
        {
            "print": {
                "ams": {
                    "ams": [
                        {
                            "id": 0,
                            "tray": [
                                {"id": 0, "tray_type": "PLA"},
                                {"id": -1, "tray_type": "PETG"},
                                {"id": 4, "tray_type": "PETG"},
                            ],
                        },
                        {"id": 1, "tray": [{"id": 0, "tray_type": "PETG"}]},
                    ]
                }
            }
        },
        "P1P",
    )
    assert [s.id for s in snapshot_from_state(1, "P1P", state).sources] == [0, 4]
    state.feed_telemetry.observe({"print": {"ams": {"ams_exist_bits": "2"}}}, "P1P")
    assert [s.id for s in snapshot_from_state(1, "P1P", state).sources] == [4]


def test_positive_presence_without_unit_details_is_unknown_not_no_ams():
    state = PrinterState(connected=True)
    state.feed_telemetry.observe({"print": {"ams": {"ams_exist_bits": "1"}}}, "P1P")
    snapshot = snapshot_from_state(1, "P1P", state)
    assert snapshot.ams_known and snapshot.ams_present and snapshot.incomplete
    assert not snapshot.sources


def test_a2l_wire_unit_is_normalized_and_disconnect_uses_wire_bit():
    state = PrinterState(connected=True)
    state.feed_telemetry.observe(
        {"print": {"ams": {"ams_exist_bits": "10000", "ams": [{"id": 16, "tray": [{"id": 0, "tray_type": "PLA"}]}]}}},
        "P1P",
    )
    assert [s.id for s in snapshot_from_state(1, "P1P", state).sources] == [24]
    state.feed_telemetry.observe({"print": {"ams": {"ams_exist_bits": "1"}}}, "P1P")
    assert not snapshot_from_state(1, "P1P", state).sources


def test_external_report_alone_cannot_authorize_no_ams_wire_encoding():
    from backend.app.services.filament_routing import RoutingPolicy, resolve_filament_routing
    from backend.tests.unit.services.test_filament_routing import requirements

    state = PrinterState(connected=True)
    state.feed_telemetry.observe({"print": {"vt_tray": {"id": 254, "tray_type": "PLA", "tray_color": "FF0000"}}}, "P1P")
    pending = resolve_filament_routing(requirements({}), RoutingPolicy(), snapshot_from_state(1, "P1P", state))
    assert pending.status == "unknown" and pending.reason == "feed_state_unavailable" and pending.plan is None
    state.feed_telemetry.observe({"print": {"ams": {"ams": []}}}, "P1P")
    assert (
        resolve_filament_routing(requirements({}), RoutingPolicy(), snapshot_from_state(1, "P1P", state)).plan.use_ams
        is False
    )


def _ams_slot(state, color, variant, material="PETG"):
    state.feed_telemetry.observe(
        {
            "print": {
                "ams": {
                    "ams": [
                        {
                            "id": 0,
                            "tray": [{"id": 1, "tray_type": material, "tray_color": color, "tray_info_idx": variant}],
                        }
                    ]
                }
            }
        },
        "P1S",
    )


def test_overlay_returns_the_actual_spool_and_moves_the_revision():
    from backend.app.services.ams_advertised_overlay import OverlayEntry
    from backend.app.services.filament_routing import RoutingPolicy, resolve_filament_routing
    from backend.tests.unit.services.test_filament_routing import requirements

    state = PrinterState(connected=True, connection_generation=1)
    _ams_slot(state, "000000FF", "GFG99")
    plain = snapshot_from_state(1, "P1S", state)
    entry = OverlayEntry("PETG", "FF0000FF", "GFG00", (), "000000FF", "GFG99", "internal")
    overlaid = snapshot_from_state(1, "P1S", state, overlay={(0, 1): entry})
    assert plain.sources[0].color == "000000FF" and overlaid.sources[0].color == "FF0000FF"
    assert overlaid.sources[0].variant == "GFG00" and overlaid.sources[0].identity == plain.sources[0].identity
    assert overlaid.revision != plain.revision

    red_job = requirements({"type": "PETG", "color": "#FF0000", "tray_info_idx": "GFG00"}, model="P1S")
    strict = RoutingPolicy(force_color_match=True, allow_base_material_match=False)
    assert resolve_filament_routing(red_job, strict, plain).status != "compatible"
    assert resolve_filament_routing(red_job, strict, overlaid).status == "compatible"


def test_a_dormant_overlay_entry_is_not_applied():
    from backend.app.services.ams_advertised_overlay import OverlayEntry

    state = PrinterState(connected=True, connection_generation=1)
    # The slot shows FF0000FF/GFG00, which is NOT what this entry advertised
    # (000000FF/GFG99) — the printer has not echoed our push, or somebody
    # reconfigured the slot on its screen. The actual values are a third pair,
    # so applying the entry would visibly change the source: with actual ==
    # live, dropping the ``matches_live`` guard would still pass.
    _ams_slot(state, "FF0000FF", "GFG00")
    entry = OverlayEntry("PETG", "00FF00FF", "GFA00", (), "000000FF", "GFG99", "internal")
    dormant = snapshot_from_state(1, "P1S", state, overlay={(0, 1): entry})
    assert (dormant.sources[0].color, dormant.sources[0].variant) == ("FF0000FF", "GFG00")
    # And a dormant entry is not a change: it must not move the revision, or
    # every routing decision would be invalidated for nothing.
    assert dormant.revision == snapshot_from_state(1, "P1S", state).revision


def test_a_strict_variant_job_does_not_match_the_generic_the_slot_advertises():
    """Generic mode masks the VARIANT, and a strict job asks for exactly that.

    Mirrors the colour case above: the spool is PLA Matte, the slot is
    advertised as Generic PLA, and with ``allow_base_material_match=False`` the
    job is compatible only when routing can see through the mask.
    """
    from backend.app.services.ams_advertised_overlay import OverlayEntry
    from backend.app.services.filament_routing import RoutingPolicy, resolve_filament_routing
    from backend.tests.unit.services.test_filament_routing import requirements

    state = PrinterState(connected=True, connection_generation=1)
    _ams_slot(state, "FF0000FF", "GFL99", material="PLA")  # Generic PLA, as advertised
    plain = snapshot_from_state(1, "P1S", state)
    entry = OverlayEntry("PLA", "FF0000FF", "GFA01", (), "FF0000FF", "GFL99", "internal")  # really PLA Matte
    overlaid = snapshot_from_state(1, "P1S", state, overlay={(0, 1): entry})

    matte_job = requirements({"type": "PLA", "color": "#FF0000", "tray_info_idx": "GFA01"}, model="P1S")
    strict = RoutingPolicy(force_color_match=True, allow_base_material_match=False)
    assert resolve_filament_routing(matte_job, strict, plain).status != "compatible"
    assert resolve_filament_routing(matte_job, strict, overlaid).status == "compatible"
