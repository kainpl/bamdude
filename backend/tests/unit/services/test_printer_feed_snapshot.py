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


def test_upgrade_kit_changes_model_and_revision_without_changing_physical_feed():
    state = PrinterState(connected=True)
    before = snapshot_from_state(1, "P1P", state)
    state.upgrade_kit_supported = True
    still_p1p = snapshot_from_state(1, "P1P", state)
    assert still_p1p.model == "P1P" and still_p1p.revision == before.revision
    state.upgrade_kit_installed = True
    upgraded = snapshot_from_state(1, "P1P", state)
    assert upgraded.model == "P1S" and upgraded.revision != before.revision
    assert upgraded.nozzle_diameters == before.nozzle_diameters


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
    # The fixed hotend reports as id 1 and IS extruder 1; the docks belong to the
    # rack carriage, extruder 0 (BambuStudio's MAIN; upstream 45dc139c).
    assert snap.nozzle_diameters == {1: (0.4,), 0: (0.4, 0.6)}


def test_h2c_hotend_ids_are_extruder_ids():
    # nozzle.info ids 0/1 are the hotends on MQTT extruders 0 (right, the rack
    # carriage) and 1 (left, fixed) — the same numbering as every H2. Only the
    # docks (16-21) need placing: on the carriage that fetches from them, 0.
    state = PrinterState(connected=True)
    state.feed_telemetry.observe(
        {
            "print": {
                "device": {
                    "nozzle": {
                        "info": [
                            {"id": 0, "diameter": "0.2"},
                            {"id": 1, "diameter": "0.6"},
                            {"id": 18, "diameter": "0.8"},
                        ]
                    }
                }
            }
        },
        "H2C",
    )
    snap = snapshot_from_state(1, "H2C", state)
    assert snap.nozzle_diameters == {0: (0.2, 0.8), 1: (0.6,)}


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
    black_job = requirements({"type": "PETG", "color": "#000000", "tray_info_idx": "GFG99"}, model="P1S")
    strict = RoutingPolicy(force_color_match=True, allow_base_material_match=False)
    # The owner's rule (2026-09-30): what the operator declared outranks the
    # job's exact checks, so the overlaid slot is judged as the black generic.
    assert resolve_filament_routing(red_job, strict, overlaid).status != "compatible"
    assert resolve_filament_routing(black_job, strict, overlaid).status == "compatible"


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


def test_an_applied_advertisement_is_carried_beside_the_spool():
    from backend.app.services.ams_advertised_overlay import OverlayEntry

    state = PrinterState(connected=True, connection_generation=1)
    _ams_slot(state, "000000FF", "GFG99")
    entry = OverlayEntry("PETG", "FF0000FF", "GFG00", (), "000000FF", "GFG99", "internal")
    source = snapshot_from_state(1, "P1S", state, overlay={(0, 1): entry}).sources[0]
    assert (source.color, source.variant) == ("FF0000FF", "GFG00")
    assert (source.declared_color, source.declared_variant) == ("000000FF", "GFG99")
    assert (source.rule_color, source.rule_variant) == ("000000FF", "GFG99")


def test_a_slot_without_an_applied_advertisement_declares_nothing():
    state = PrinterState(connected=True, connection_generation=1)
    _ams_slot(state, "FF0000FF", "GFG00")
    source = snapshot_from_state(1, "P1S", state).sources[0]
    assert source.declared_color is None and source.declared_variant is None
    assert (source.rule_color, source.rule_variant) == ("FF0000FF", "GFG00")


def test_a_strict_variant_job_is_judged_by_the_generic_the_slot_advertises():
    """Generic mode masks the VARIANT, and the owner's rule (2026-09-30) makes
    the declared Generic outrank the strict profile.

    Mirrors the colour case above: the spool is PLA Matte, the slot is
    advertised as Generic PLA, and with ``allow_base_material_match=False`` a
    job asking for Generic PLA fits it while one asking for PLA Matte does not.
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
    generic_job = requirements({"type": "PLA", "color": "#FF0000", "tray_info_idx": "GFL99"}, model="P1S")
    strict = RoutingPolicy(force_color_match=True, allow_base_material_match=False)
    assert resolve_filament_routing(matte_job, strict, plain).status != "compatible"
    assert resolve_filament_routing(matte_job, strict, overlaid).status != "compatible"
    assert resolve_filament_routing(generic_job, strict, overlaid).status == "compatible"


def _nozzle_state(info, model="H2C"):
    state = PrinterState(connected=True)
    state.feed_telemetry.observe({"print": {"device": {"nozzle": {"info": info}}}}, model)
    return snapshot_from_state(1, model, state)


def test_an_empty_hotend_does_not_count_its_last_nozzle():
    # upstream 4961990a (#2885): a hotend that parked its nozzle back in the
    # rack keeps reporting that nozzle's diameter, with serial "N/A" and no
    # temperature rating. Counting it let a slice match a nozzle the machine
    # did not have mounted.
    snap = _nozzle_state(
        [
            {"id": 0, "diameter": "0.6", "sn": "N/A", "tm": 0},
            {"id": 1, "diameter": "0.4", "sn": "SN123", "tm": 300},
            {"id": 16, "diameter": "0.2"},
        ]
    )
    assert snap.nozzle_diameters == {1: (0.4,), 0: (0.2,)}


def test_emptiness_has_to_be_stated():
    # A firmware that reports neither field has told us nothing: that is not
    # an empty hotend, and reading it as one would switch the guard off.
    snap = _nozzle_state(
        [{"id": 0, "diameter": "0.4"}, {"id": 1, "diameter": "0.4", "serial_number": "N/A", "max_temp": 300}], "H2D"
    )
    assert snap.nozzle_diameters == {0: (0.4,), 1: (0.4,)}


def test_h2d_fixed_hotends_are_not_treated_as_empty():
    # H2D 01.03.00.00 reports both installed fixed hotends with the same
    # sn/tm fields that identify an empty rack-capable H2C hotend.  Unlike an
    # H2C, an H2D cannot park either nozzle, so both diameters are authoritative.
    snap = _nozzle_state(
        [
            {"id": 0, "diameter": 0.4, "sn": "N/A", "stat": 5, "tm": 0, "type": "HS01"},
            {"id": 1, "diameter": 0.4, "sn": "N/A", "stat": 5, "tm": 0, "type": "HS01"},
        ],
        "H2D",
    )
    assert snap.nozzle_diameters == {0: (0.4,), 1: (0.4,)}


def test_a_present_ams_with_no_spool_offers_no_source_whatever_power_on_flag_says():
    """``power_on_flag`` is the "read spools on power-on" setting, not the printer's power.

    A push that reports the unit present (``ams_exist_bits``) and no spool in it
    (``tray_exist_bits`` 0) empties the slots even with the setting off; the
    printer-off shape, with no unit reported, still leaves them alone.
    """
    loaded = {
        "ams_exist_bits": "1",
        "tray_exist_bits": "3",
        "power_on_flag": False,
        "ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}, {"id": 1, "tray_type": "PETG"}]}],
    }
    state = PrinterState(connected=True)
    state.feed_telemetry.observe({"print": {"ams": loaded}}, "P1S")
    assert [s.id for s in snapshot_from_state(1, "P1S", state).sources] == [0, 1]

    state.feed_telemetry.observe({"print": {"ams": {"tray_exist_bits": "0", "power_on_flag": False}}}, "P1S")
    assert [s.id for s in snapshot_from_state(1, "P1S", state).sources] == [0, 1]

    state.feed_telemetry.observe(
        {"print": {"ams": {"ams_exist_bits": "1", "tray_exist_bits": "0", "power_on_flag": False}}}, "P1S"
    )
    assert snapshot_from_state(1, "P1S", state).sources == ()
