"""Tests for the G3 (AMS) upstream port — v0.2.4.9 → v1.2.5 audit cycle.

Covers, one class per upstream item:

* ``TestHtPartialStateUpdate``   — #2594: a partial ``{id, state=9}`` from an
  AMS-HT must not wipe the loaded spool.
* ``TestTrayExistBitsAnnotate``  — #2527: ``tray_exist_bits`` annotates an
  authoritative ``exists`` bool so a non-RFID spool reads "?" not "Empty".
* ``TestTrayTarPreParsing``      — #2587: ``tray_tar``/``tray_pre`` are captured
  raw off the AMS payload.
* ``TestResolveExpectedTray``    — #2587: raw slot → global tray id resolution.
* ``TestAssignmentVerification`` — #2582: assignment read-back confirm/timeout.
* ``TestA2LAmsLite``             — A2L AMS-Lite unit id 16 → 6 normalisation and
  the outbound 6 → 16 wire translation.
* ``TestNozzleMismatchGuard``    — #1899: pre-dispatch sliced-vs-installed nozzle
  guard is fail-safe and only blocks on a positive mismatch.
"""

import pytest

from backend.app.services.bambu_mqtt import (
    A2L_LITE_GLOBAL_BASE,
    A2L_LITE_NORMALIZED_AMS_ID,
    A2L_LITE_PHYSICAL_AMS_ID,
    BambuMQTTClient,
    a2l_lite_wire_ids,
    apply_tray_exist_bits,
    normalize_am_unit_id,
)
from backend.app.services.print_scheduler import (
    _installed_nozzle_diameters,
    _nozzle_mismatch_message,
)
from backend.app.services.printer_manager import resolve_expected_tray


@pytest.fixture
def client():
    return BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST123",
        access_code="12345678",
    )


def _loaded_tray(tray_id=0, **over):
    tray = {
        "id": tray_id,
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Basic",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFA00",
        "tag_uid": "1122334455667788",
        "remain": 80,
        "state": 11,
    }
    tray.update(over)
    return tray


# ── #2594 ────────────────────────────────────────────────────────────────────


class TestHtPartialStateUpdate:
    def test_ht_partial_state_9_keeps_spool(self, client):
        """An AMS-HT reports its LOADED tray as state=9 — the regular-AMS
        'state != 11 means emptied' heuristic must not apply (#2594)."""
        client.state.raw_data["ams"] = [{"id": 128, "tray": [_loaded_tray(0, state=9)]}]

        client._handle_ams_data({"ams": [{"id": 128, "tray": [{"id": 0, "state": 9}]}]})

        tray = client.state.raw_data["ams"][0]["tray"][0]
        assert tray["tray_type"] == "PLA"
        assert tray["tray_info_idx"] == "GFA00"
        assert tray["tag_uid"] == "1122334455667788"

    def test_regular_ams_partial_state_9_still_clears(self, client):
        """Regular AMS (id < 128) keeps the original #784 clearing behaviour."""
        client.state.raw_data["ams"] = [{"id": 0, "tray": [_loaded_tray(0)]}]

        client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0, "state": 9}]}]})

        tray = client.state.raw_data["ams"][0]["tray"][0]
        assert tray["tray_type"] == ""
        assert tray["tray_info_idx"] == ""

    def test_ht_explicit_empty_tray_type_still_clears(self, client):
        """Genuine HT removal arrives as an explicit ``tray_type=""``."""
        client.state.raw_data["ams"] = [{"id": 128, "tray": [_loaded_tray(0, state=9)]}]

        client._handle_ams_data({"ams": [{"id": 128, "tray": [{"id": 0, "state": 9, "tray_type": ""}]}]})

        assert client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == ""


# ── #2527 ────────────────────────────────────────────────────────────────────


class TestTrayExistBitsAnnotate:
    def test_annotate_exists_marks_present_and_absent(self):
        """bits=0x5 → slots 0,2 present; 1,3 absent."""
        units = [{"id": 0, "tray": [{"id": i} for i in range(4)]}]

        apply_tray_exist_bits(units, "5", annotate_exists=True)

        assert [t["exists"] for t in units[0]["tray"]] == [True, False, True, False]

    def test_present_but_unidentified_slot_keeps_exists_true(self):
        """The #2527 case: a non-RFID spool is present (bit 1) but reports an
        empty tray_type + state=9 — ``exists`` must still be True so the UI can
        render "?" rather than "Empty"."""
        units = [{"id": 0, "tray": [{"id": 0, "tray_type": "", "state": 9}]}]

        apply_tray_exist_bits(units, "f", annotate_exists=True)

        assert units[0]["tray"][0]["exists"] is True

    def test_no_annotation_without_flag(self):
        """The VP bridge leaves the flag off, so ``exists`` never reaches the
        slicer wire format."""
        units = [{"id": 0, "tray": [{"id": i} for i in range(4)]}]

        apply_tray_exist_bits(units, "5")

        assert all("exists" not in t for t in units[0]["tray"])


# ── #2587 ────────────────────────────────────────────────────────────────────


class TestTrayTarPreParsing:
    def test_parses_string_values(self, client):
        client._handle_ams_data({"tray_tar": "2", "tray_pre": "1"})
        assert client.state.tray_tar == 2
        assert client.state.tray_pre == 1

    def test_parses_int_values(self, client):
        client._handle_ams_data({"tray_tar": 3, "tray_pre": 0})
        assert client.state.tray_tar == 3
        assert client.state.tray_pre == 0

    def test_unparseable_falls_back_to_sentinel(self, client):
        client._handle_ams_data({"tray_tar": "not-a-number"})
        assert client.state.tray_tar == 255

    def test_absent_keys_leave_previous_value(self, client):
        client._handle_ams_data({"tray_tar": 2})
        client._handle_ams_data({"tray_now": "2"})
        assert client.state.tray_tar == 2


class TestResolveExpectedTray:
    SINGLE = [(0, False)]
    MULTI = [(0, False), (1, False)]

    def test_idle_sentinels_are_none(self):
        assert resolve_expected_tray(255, self.SINGLE, None) is None
        assert resolve_expected_tray(-1, self.SINGLE, None) is None
        assert resolve_expected_tray(None, self.SINGLE, None) is None

    def test_external_passthrough(self):
        assert resolve_expected_tray(254, self.SINGLE, None) == 254

    def test_ams_ht_passthrough(self):
        assert resolve_expected_tray(129, [(129, True)], None) == 129

    def test_single_ams_local_slot_globalised(self):
        assert resolve_expected_tray(2, [(1, False)], None) == 6

    def test_multi_ams_resolved_via_mapping(self):
        # snow-encoded: ams_hw_id*256 + slot → AMS 1 slot 2 == 1*256+2 == 258
        assert resolve_expected_tray(2, self.MULTI, [258]) == 6

    def test_multi_ams_ambiguous_is_none(self):
        """Two AMS both claiming slot 2 → honest None, never a wrong slot."""
        assert resolve_expected_tray(2, self.MULTI, [2, 258]) is None

    def test_multi_ams_without_mapping_is_none(self):
        assert resolve_expected_tray(2, self.MULTI, None) is None

    def test_no_regular_ams_is_none(self):
        assert resolve_expected_tray(2, [(128, True)], None) is None

    def test_already_global_regular_ams(self):
        assert resolve_expected_tray(9, self.MULTI, None) == 9

    def test_a2l_lite_range_passthrough(self):
        assert resolve_expected_tray(26, [(6, False)], None) == 26

    def test_out_of_range_is_none(self):
        assert resolve_expected_tray(200, self.SINGLE, None) is None


# ── #2582 ────────────────────────────────────────────────────────────────────


class TestAssignmentVerification:
    def test_blank_filament_id_registers_nothing(self, client):
        client.register_assignment_verification(0, 1, "", "FF0000FF", 3)
        assert client._pending_assignments == {}

    def test_match_fires_verified(self, client):
        fired = []
        client.on_assignment_verified = lambda *a: fired.append(a)
        client.register_assignment_verification(0, 1, "GFA00", "FF0000FF", None)
        client.state.raw_data["ams"] = [{"id": 0, "tray": [{"id": 1, "tray_info_idx": "GFA00"}]}]

        client._check_assignment_verifications()

        assert len(fired) == 1
        ams_id, tray_id, verified, detail = fired[0]
        assert (ams_id, tray_id, verified) == (0, 1, True)
        assert detail["kprofile_applied"] is True
        assert client._pending_assignments == {}

    def test_kprofile_mismatch_flagged(self, client):
        fired = []
        client.on_assignment_verified = lambda *a: fired.append(a)
        client.register_assignment_verification(0, 1, "GFA00", "FF0000FF", 3)
        client.state.raw_data["ams"] = [{"id": 0, "tray": [{"id": 1, "tray_info_idx": "GFA00", "cali_idx": 7}]}]

        client._check_assignment_verifications()

        assert fired[0][2] is True
        assert fired[0][3]["kprofile_applied"] is False

    def test_note_cali_idx_fills_pending(self, client):
        """BamDude divergence: our assign path resolves cali_idx live inside
        ``apply_active_calibration_to_slot``, which hands it back here."""
        client.register_assignment_verification(0, 1, "GFA00", "FF0000FF", None)
        client.note_assignment_cali_idx(0, 1, 5)
        assert client._pending_assignments[(0, 1)]["cali_idx"] == 5

    def test_note_cali_idx_is_noop_without_pending(self, client):
        client.note_assignment_cali_idx(0, 1, 5)
        assert client._pending_assignments == {}

    def test_wrong_id_within_window_stays_pending(self, client):
        fired = []
        client.on_assignment_verified = lambda *a: fired.append(a)
        client.register_assignment_verification(0, 1, "GFA00", "FF0000FF", None)
        client.state.raw_data["ams"] = [{"id": 0, "tray": [{"id": 1, "tray_info_idx": "GFB99"}]}]

        client._check_assignment_verifications()

        assert fired == []
        assert (0, 1) in client._pending_assignments

    def test_timeout_fires_not_verified(self, client):
        fired = []
        client.on_assignment_verified = lambda *a: fired.append(a)
        client.register_assignment_verification(0, 1, "GFA00", "FF0000FF", None)
        client.state.raw_data["ams"] = [{"id": 0, "tray": [{"id": 1, "tray_info_idx": "GFB99"}]}]
        client._pending_assignments[(0, 1)]["deadline"] = 0.0

        client._check_assignment_verifications()

        assert fired[0][2] is False
        assert fired[0][3]["saw_tray"] is True
        assert fired[0][3]["actual_tray_info_idx"] == "GFB99"

    def test_timeout_without_any_telemetry_reports_saw_tray_false(self, client):
        fired = []
        client.on_assignment_verified = lambda *a: fired.append(a)
        client.register_assignment_verification(0, 1, "GFA00", "FF0000FF", None)
        client._pending_assignments[(0, 1)]["deadline"] = 0.0

        client._check_assignment_verifications()

        assert fired[0][2] is False
        assert fired[0][3]["saw_tray"] is False

    def test_external_slot_resolved_from_vt_tray(self, client):
        fired = []
        client.on_assignment_verified = lambda *a: fired.append(a)
        client.register_assignment_verification(255, 0, "GFA00", "FF0000FF", None)
        client.state.raw_data["vt_tray"] = [{"id": 254, "tray_info_idx": "GFA00"}]

        client._check_assignment_verifications()

        assert fired[0][2] is True

    def test_ht_single_tray_falls_back_to_sole_tray(self, client):
        fired = []
        client.on_assignment_verified = lambda *a: fired.append(a)
        client.register_assignment_verification(128, 0, "GFA00", "FF0000FF", None)
        # HT reports its lone tray under a non-matching id
        client.state.raw_data["ams"] = [{"id": 128, "tray": [{"id": 4, "tray_info_idx": "GFA00"}]}]

        client._check_assignment_verifications()

        assert fired[0][2] is True


# ── A2L AMS Lite ─────────────────────────────────────────────────────────────


class TestA2LAmsLite:
    def test_normalize_only_touches_16(self):
        assert normalize_am_unit_id(A2L_LITE_PHYSICAL_AMS_ID) == A2L_LITE_NORMALIZED_AMS_ID
        for other in (0, 1, 2, 3, 6, 128, 135, 255):
            if other != A2L_LITE_PHYSICAL_AMS_ID:
                assert normalize_am_unit_id(other) == other

    def test_wire_ids_only_for_normalised_unit(self):
        assert a2l_lite_wire_ids(0, 1) is None
        assert a2l_lite_wire_ids(128, 0) is None
        assert a2l_lite_wire_ids(A2L_LITE_NORMALIZED_AMS_ID, 26) == (16, 2, 66)

    def test_ingest_rewrites_unit_id(self, client):
        client._handle_ams_data({"ams": [{"id": 16, "tray": [{"id": i} for i in range(4)]}]})

        assert client._has_a2l_am_unit is True
        assert client.state.raw_data["ams"][0]["id"] == A2L_LITE_NORMALIZED_AMS_ID

    def test_other_units_untouched(self, client):
        client._handle_ams_data({"ams": [{"id": 0, "tray": [{"id": 0}]}]})

        assert client._has_a2l_am_unit is False
        assert client.state.raw_data["ams"][0]["id"] == 0

    def test_local_tray_now_globalised_to_24_range(self, client):
        client._handle_ams_data(
            {
                "ams": [{"id": 16, "tray": [{"id": i} for i in range(4)]}],
                "ams_exist_bits": "1",
                "tray_now": "2",
            }
        )

        assert client.state.tray_now == A2L_LITE_GLOBAL_BASE + 2

    def test_exist_bits_land_on_bit_base_24(self, client):
        """The Lite's bitmask sits at bit base 24 — after normalisation to id 6
        the ``ams_id*4+slot`` probe hits the right bits."""
        units = [{"id": A2L_LITE_NORMALIZED_AMS_ID, "tray": [{"id": i} for i in range(4)]}]

        apply_tray_exist_bits(units, hex(0b0101 << 24)[2:], annotate_exists=True)

        assert [t["exists"] for t in units[0]["tray"]] == [True, False, True, False]


# ── #1899 ────────────────────────────────────────────────────────────────────


class _Nozzle:
    def __init__(self, diameter):
        self.nozzle_diameter = diameter


class _Status:
    def __init__(self, nozzles):
        self.nozzles = nozzles


class TestNozzleMismatchGuard:
    def test_parses_reported_diameters(self):
        assert _installed_nozzle_diameters(_Status([_Nozzle("0.4"), _Nozzle("0.6")])) == [0.4, 0.6]

    def test_skips_unfilled_defaults(self):
        assert _installed_nozzle_diameters(_Status([_Nozzle(""), _Nozzle("0.6")])) == [0.6]

    def test_none_status_is_empty(self):
        assert _installed_nozzle_diameters(None) == []

    def test_no_sliced_diameter_never_blocks(self):
        assert _nozzle_mismatch_message(None, [0.4]) is None

    def test_no_reported_nozzles_never_blocks(self):
        assert _nozzle_mismatch_message(0.6, []) is None

    def test_match_passes(self):
        assert _nozzle_mismatch_message(0.4, [0.4]) is None

    def test_dual_nozzle_either_side_passes(self):
        assert _nozzle_mismatch_message(0.6, [0.4, 0.6]) is None

    def test_positive_mismatch_blocks_with_actionable_text(self):
        msg = _nozzle_mismatch_message(0.6, [0.4])
        assert msg is not None
        assert "0.6mm" in msg and "0.4mm" in msg

    def test_float_noise_tolerated(self):
        assert _nozzle_mismatch_message(0.40001, [0.4]) is None


# ── #2670 ────────────────────────────────────────────────────────────────────


class TestHtRemovalIsNoticed:
    """Pulling an AMS-HT spool must clear the slot AND fire the change callback.

    The HT signals removal only through ``tray_exist_bits``: it keeps echoing the
    old ``tray_type`` / ``tag_uid`` / ``remain``, and its ``state`` is
    firmware-variant. Both halves of upstream #2670 live here — the bit position,
    and the change hash that has to be taken over the merged state for the
    cleared slot to be visible to it.
    """

    def test_pulled_ht_spool_clears_and_notifies(self, client):
        client.state.raw_data["ams"] = [{"id": 128, "tray": [_loaded_tray(0, state=9)]}]
        seen: list = []
        client.on_ams_change = seen.append

        # Bit 16 clear = HT-A empty. The payload still carries the old spool,
        # which is exactly what the firmware does.
        client._handle_ams_data(
            {
                "ams": [{"id": 128, "tray": [_loaded_tray(0, state=9)]}],
                "tray_exist_bits": "0",
            }
        )

        tray = client.state.raw_data["ams"][0]["tray"][0]
        assert tray["tray_type"] == ""
        assert seen, "on_ams_change must fire — otherwise the spool stays bound to an empty slot"

    def test_loaded_ht_is_not_cleared(self, client):
        client.state.raw_data["ams"] = [{"id": 128, "tray": [_loaded_tray(0, state=9)]}]

        client._handle_ams_data(
            {
                "ams": [{"id": 128, "tray": [_loaded_tray(0, state=9)]}],
                "tray_exist_bits": hex(1 << 16),
            }
        )

        assert client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "PLA"


class TestOutgoingPublishesAreTeed:
    """The other half of a recording, wired at the one place paho is built.

    ⚠️ Not at the call sites: this class publishes from dozens of them, and the
    single method that already carried an outgoing hook is not the one the
    commands worth reading go through.
    """

    class _Paho:
        def __init__(self):
            self.sent = []

        def publish(self, topic, payload=None, qos=0, retain=False, properties=None):
            self.sent.append((topic, payload, qos))
            return "ack"

    def test_a_publish_reaches_both_the_printer_and_the_listener(self, client):
        paho = self._Paho()
        client._tee_publishes(paho)
        seen = []
        client.register_raw_publish_handler(lambda t, p: seen.append((t, p)))

        result = paho.publish("device/X/request", '{"print":{"command":"x"}}', qos=1)

        assert result == "ack", "the real publish must still happen and its result pass through"
        assert paho.sent == [("device/X/request", '{"print":{"command":"x"}}', 1)]
        assert seen == [("device/X/request", b'{"print":{"command":"x"}}')]

    def test_a_crashing_listener_cannot_stop_a_command(self, client):
        """⚠️ A debugging aid must never be able to keep a command from the printer."""
        paho = self._Paho()
        client._tee_publishes(paho)

        def _boom(_t, _p):
            raise RuntimeError("nope")

        client.register_raw_publish_handler(_boom)

        paho.publish("device/X/request", "{}")

        assert len(paho.sent) == 1

    def test_nothing_is_teed_once_unregistered(self, client):
        paho = self._Paho()
        client._tee_publishes(paho)
        seen = []
        handler = lambda t, p: seen.append(t)  # noqa: E731
        client.register_raw_publish_handler(handler)
        client.unregister_raw_publish_handler(handler)

        paho.publish("device/X/request", "{}")

        assert seen == []


class TestTheTeeIsActuallyAttached:
    """⚠️ ``_tee_publishes`` being correct proves nothing if nobody calls it.

    Removing the one call in ``connect`` left every other test in this file
    green, which is exactly how a recording would quietly lose its outgoing
    half again.
    """

    def test_connect_wraps_the_paho_client_it_builds(self, client, monkeypatch):
        from unittest.mock import MagicMock

        built = MagicMock()
        monkeypatch.setattr("backend.app.services.bambu_mqtt.mqtt.Client", MagicMock(return_value=built))

        client.connect()

        seen = []
        client.register_raw_publish_handler(lambda t, p: seen.append(t))
        client._client.publish("device/X/request", "{}")

        assert seen == ["device/X/request"], "connect() built a client nobody tees"
