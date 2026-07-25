"""Tests for the VP MQTT bridge — non-proxy mirror of target printer state to slicer."""

import asyncio
import json
import logging
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.virtual_printer.mqtt_bridge import (
    MQTTBridge,
    _ip_to_uint32_le,
    _resolve_target_to_ipv4,
)
from backend.app.services.virtual_printer.mqtt_server import SimpleMQTTServer

H2D_SERIAL = "0948BB540200427"
VP_SERIAL = "09400A391800003"
H2D_IP = "192.168.255.133"
VP_IP = "192.168.255.16"


def _make_server(serial: str = VP_SERIAL, bind_address: str = VP_IP) -> SimpleMQTTServer:
    return SimpleMQTTServer(
        serial=serial,
        access_code="deadbeef",
        cert_path=Path("/tmp/unused.crt"),  # nosec B108
        key_path=Path("/tmp/unused.key"),  # nosec B108
        model="O1D",
        bind_address=bind_address,
    )


def _make_paho_client(
    serial: str = H2D_SERIAL,
    ip: str = H2D_IP,
    *,
    connected: bool = True,
) -> MagicMock:
    """Build a mock BambuMQTTClient that satisfies MQTTBridge's interface."""
    client = MagicMock()
    client.serial_number = serial
    client.ip_address = ip
    client.state = MagicMock()
    client.state.connected = connected
    client.publish_raw = MagicMock(return_value=True)
    client._raw_handlers: list = []

    def _register(handler):
        client._raw_handlers.append(handler)

    def _unregister(handler):
        if handler in client._raw_handlers:
            client._raw_handlers.remove(handler)

    client.register_raw_message_handler.side_effect = _register
    client.unregister_raw_message_handler.side_effect = _unregister
    # No-op for _request_version / request_status_update so the post-bind nudge doesn't crash.
    client._request_version = MagicMock()
    client.request_status_update = MagicMock()
    return client


def _make_printer_manager(client) -> MagicMock:
    pm = MagicMock()
    pm.get_client = MagicMock(return_value=client)
    return pm


def _make_bridge(server: SimpleMQTTServer, target: MagicMock | None = None) -> MQTTBridge:
    target = target if target is not None else _make_paho_client()
    pm = _make_printer_manager(target)
    return MQTTBridge(
        vp_id=1,
        vp_name="vp1",
        vp_serial=VP_SERIAL,
        target_printer_id=42,
        mqtt_server=server,
        printer_manager=pm,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestBridgeLifecycle:
    @pytest.mark.asyncio
    async def test_start_registers_handler_on_target_client(self):
        target = _make_paho_client()
        bridge = _make_bridge(_make_server(), target)
        await bridge.start()
        assert len(target._raw_handlers) == 1
        assert bridge.is_active is True
        await bridge.stop()
        assert len(target._raw_handlers) == 0

    @pytest.mark.asyncio
    async def test_start_with_no_target_client_does_not_crash(self):
        pm = MagicMock()
        pm.get_client = MagicMock(return_value=None)
        bridge = MQTTBridge(
            vp_id=1,
            vp_name="vp1",
            vp_serial=VP_SERIAL,
            target_printer_id=42,
            mqtt_server=_make_server(),
            printer_manager=pm,
        )
        await bridge.start()
        assert bridge.is_active is False
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_resolve_rebinds_when_paho_client_replaced(self):
        """BambuMQTTClient is destroyed and recreated on connect_printer; bridge must rebind."""
        old_client = _make_paho_client(serial="REAL_OLD")
        new_client = _make_paho_client(serial="REAL_NEW")
        pm = _make_printer_manager(old_client)
        bridge = MQTTBridge(
            vp_id=1,
            vp_name="vp1",
            vp_serial=VP_SERIAL,
            target_printer_id=42,
            mqtt_server=_make_server(),
            printer_manager=pm,
        )
        await bridge.start()
        assert len(old_client._raw_handlers) == 1
        assert bridge._target_serial == "REAL_OLD"

        pm.get_client.return_value = new_client
        bridge._resolve_client()
        assert len(old_client._raw_handlers) == 0
        assert len(new_client._raw_handlers) == 1
        assert bridge._target_serial == "REAL_NEW"

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_post_bind_nudge_requests_version_and_status(self):
        target = _make_paho_client()
        bridge = _make_bridge(_make_server(), target)
        await bridge.start()
        target._request_version.assert_called_once()
        target.request_status_update.assert_called_once()
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_post_bind_nudge_skipped_when_target_not_connected(self):
        """#1721: the bridge can attach before the real printer's MQTT TLS
        handshake completes. Calling request_status_update on a disconnected
        client logs WARNING (bambu_mqtt.py: "request_status_update: not
        connected"); on A1 firmware that reconnects aggressively, every bind
        cycle pollutes the support bundle with a benign line. The bridge must
        check state.connected before nudging — the next periodic pushall picks
        up the cache anyway.
        """
        target = _make_paho_client(connected=False)
        bridge = _make_bridge(_make_server(), target)
        await bridge.start()
        target._request_version.assert_not_called()
        target.request_status_update.assert_not_called()
        await bridge.stop()


# ---------------------------------------------------------------------------
# Caching: push_status
# ---------------------------------------------------------------------------


class TestPushStatusCache:
    """push_status snapshots feed `_send_status_report` via the cache, not a fan-out."""

    @pytest.mark.asyncio
    async def test_push_status_is_cached_not_fanned_out(self):
        server = _make_server()
        server.push_raw_to_clients = AsyncMock()
        bridge = _make_bridge(server)
        await bridge.start()

        payload = json.dumps({"print": {"command": "push_status", "ams": {"ams": []}, "gcode_state": "IDLE"}}).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", payload)
        await asyncio.sleep(0.01)

        server.push_raw_to_clients.assert_not_awaited()
        cached = bridge.get_latest_print_state()
        assert cached is not None
        assert cached["command"] == "push_status"
        assert cached["gcode_state"] == "IDLE"

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_serial_rewritten_in_cached_push(self):
        server = _make_server()
        bridge = _make_bridge(server)
        await bridge.start()

        payload = json.dumps(
            {
                "print": {
                    "command": "push_status",
                    "upgrade_state": {"sn": H2D_SERIAL, "status": "IDLE"},
                }
            }
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", payload)
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        assert cached["upgrade_state"]["sn"] == VP_SERIAL

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_net_info_ip_rewritten_to_vp_ip(self):
        """BambuStudio reads `net.info[].ip` (LE uint32) for the FTP destination —
        must be rewritten to the VP's bind IP or the slicer bypasses the VP."""
        server = _make_server(bind_address=VP_IP)
        bridge = _make_bridge(server)
        await bridge.start()

        h2d_le = _ip_to_uint32_le(H2D_IP)
        vp_le = _ip_to_uint32_le(VP_IP)
        payload = json.dumps(
            {
                "print": {
                    "command": "push_status",
                    "net": {"info": [{"ip": h2d_le, "mask": 0xFFFFFF}, {"ip": 0, "mask": 0}]},
                }
            }
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", payload)
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        assert cached["net"]["info"][0]["ip"] == vp_le
        assert cached["net"]["info"][1]["ip"] == 0  # untouched

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_every_active_interface_is_rewritten_not_just_the_tracked_one(self):
        """Multi-NIC leak. A printer on WiFi *and* Ethernet reports two active
        `net.info` entries with different IPs, and only one is the address we
        connect on. Rewriting just that one hands BambuStudio a live route
        straight to the printer, past the VP — the #1429 / #1302 symptom. Every
        non-zero entry must land on the VP; zero entries are inactive
        placeholders and stay put."""
        server = _make_server(bind_address=VP_IP)
        bridge = _make_bridge(server)
        await bridge.start()

        vp_le = _ip_to_uint32_le(VP_IP)
        wifi_le = _ip_to_uint32_le(H2D_IP)  # the interface we connect on
        eth_le = _ip_to_uint32_le("192.168.7.42")  # second active NIC
        payload = json.dumps(
            {
                "print": {
                    "command": "push_status",
                    "net": {
                        "info": [
                            {"ip": wifi_le, "mask": 0xFFFFFF},
                            {"ip": eth_le, "mask": 0xFFFFFF},
                            {"ip": 0, "mask": 0},
                        ]
                    },
                }
            }
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", payload)
        await asyncio.sleep(0.01)

        info = bridge.get_latest_print_state()["net"]["info"]
        assert info[0]["ip"] == vp_le
        assert info[1]["ip"] == vp_le, "the secondary interface leaked the real printer IP"
        assert info[2]["ip"] == 0  # placeholder untouched

    @pytest.mark.asyncio
    async def test_encoding_rearms_on_a_later_tick_and_sweeps_the_cache(self):
        """The encoding used to be computed only when the client OBJECT changed,
        so a client built before its `ip_address` was known stayed disarmed for
        its whole life — `ensure_fresh_connection` is on-demand only, so an idle
        printer never rebuilt it. The refresh loop now re-encodes every tick,
        and a newly-armed encoding sweeps the already-cached push_status, which
        would otherwise carry the poisoned IP forward across incrementals."""
        server = _make_server(bind_address=VP_IP)
        bridge = _make_bridge(server)
        client = bridge._printer_manager.get_client(bridge.target_printer_id)
        client.ip_address = None  # not known yet at bind time
        await bridge.start()
        assert bridge._vp_ip_uint32_le is None, "must not arm without a target IP"

        # A push arrives while disarmed — the real IP gets cached as-is.
        h2d_le = _ip_to_uint32_le(H2D_IP)
        payload = json.dumps(
            {"print": {"command": "push_status", "net": {"info": [{"ip": h2d_le, "mask": 0xFFFFFF}]}}}
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", payload)
        await asyncio.sleep(0.01)
        assert bridge.get_latest_print_state()["net"]["info"][0]["ip"] == h2d_le

        # DHCP/DNS settles; the same client object now knows its address.
        client.ip_address = H2D_IP
        bridge._resolve_client()

        assert bridge._vp_ip_uint32_le == _ip_to_uint32_le(VP_IP)
        assert bridge.get_latest_print_state()["net"]["info"][0]["ip"] == _ip_to_uint32_le(VP_IP)

    @pytest.mark.asyncio
    async def test_net_info_ip_rewritten_when_bind_is_wildcard(self):
        """#1429 residual: with bind_address=0.0.0.0 (flat-LAN default) the
        encoding must still arm by auto-resolving the host interface, otherwise
        net.info leaks the real printer IP and Send bypasses the VP."""
        server = _make_server(bind_address="0.0.0.0")
        bridge = _make_bridge(server)

        h2d_le = _ip_to_uint32_le(H2D_IP)
        vp_le = _ip_to_uint32_le(VP_IP)
        with patch(
            "backend.app.services.virtual_printer.mqtt_bridge._resolve_host_interface_for_target",
            return_value=VP_IP,
        ):
            await bridge.start()

        payload = json.dumps(
            {"print": {"command": "push_status", "net": {"info": [{"ip": h2d_le, "mask": 0xFFFFFF}]}}}
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", payload)
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        assert cached["net"]["info"][0]["ip"] == vp_le  # auto-resolved, not leaked
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_net_info_encoding_stays_unarmed_when_no_interface_matches(self):
        """If auto-resolve finds no host interface in the printer's subnet the
        encoding stays unarmed and the (leaky) pass-through behaviour stands —
        we never write a bogus IP into net.info."""
        server = _make_server(bind_address="0.0.0.0")
        bridge = _make_bridge(server)

        h2d_le = _ip_to_uint32_le(H2D_IP)
        with patch(
            "backend.app.services.virtual_printer.mqtt_bridge._resolve_host_interface_for_target",
            return_value=None,
        ):
            await bridge.start()

        assert bridge._vp_ip_uint32_le is None
        payload = json.dumps(
            {"print": {"command": "push_status", "net": {"info": [{"ip": h2d_le, "mask": 0xFFFFFF}]}}}
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", payload)
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        assert cached["net"]["info"][0]["ip"] == h2d_le  # untouched pass-through
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_request_topic_message_is_ignored(self):
        server = _make_server()
        bridge = _make_bridge(server)
        await bridge.start()

        payload = json.dumps({"print": {"command": "push_status"}}).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/request", payload)
        await asyncio.sleep(0.01)

        assert bridge.get_latest_print_state() is None
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_incremental_push_preserves_non_allowlisted_capability_fields(self):
        """Regression for upstream Bambuddy #1622 round 4: BambuStudio gates
        Device-tab UIs (manage calibration, AMS-slot filament dropdown, ...) on
        capability / lifecycle fields (``cali_version``, ``print_type``,
        ``mc_print_stage``, ``device``, ...) it reads off the cached
        push_status. Before the fix these fields were not in the sticky-key
        allowlist and drained out of the bridge cache on the first 1 Hz
        incremental tick, so the slicer's Device tab greyed out the gated UIs
        once the cache thinned. After the fix the cache accumulates everything
        the printer has ever sent, dropped only when explicitly overwritten.
        """
        server = _make_server()
        bridge = _make_bridge(server)
        await bridge.start()

        full_push = json.dumps(
            {
                "print": {
                    "command": "push_status",
                    "cali_version": 2,
                    "print_type": "idle",
                    "gcode_state": "IDLE",
                    "mc_print_stage": "0",
                    "mc_stage": 0,
                    "device": {"ext_tool": {"info": []}},
                    "cfg": "",
                    "home_flag": 256,
                    "wifi_signal": "-50dBm",
                }
            }
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", full_push)
        await asyncio.sleep(0.01)

        # Incremental push carrying only temps + wifi — none of the
        # capability/lifecycle fields above are mentioned.
        incremental_push = json.dumps(
            {
                "print": {
                    "command": "push_status",
                    "wifi_signal": "-55dBm",
                    "nozzle_temper": 24.5,
                }
            }
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", incremental_push)
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        # Incremental values applied.
        assert cached["wifi_signal"] == "-55dBm"
        assert cached["nozzle_temper"] == 24.5
        # Capability / lifecycle fields preserved from the prior pushall
        # — the symptoms in #1622 (Device-tab UIs disabled) trace to these
        # exact keys missing.
        assert cached["cali_version"] == 2
        assert cached["print_type"] == "idle"
        assert cached["gcode_state"] == "IDLE"
        assert cached["mc_print_stage"] == "0"
        assert cached["mc_stage"] == 0
        assert cached["device"] == {"ext_tool": {"info": []}}
        assert cached["cfg"] == ""
        assert cached["home_flag"] == 256

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_partial_vt_tray_update_overlays_onto_cached_full_dict(self):
        """Regression for upstream Bambuddy #1622 round 5: right after the
        slicer picks a filament for the external spool (vt_tray, ams_id=255),
        Bambu firmware pushes a partial vt_tray carrying just the changed
        fields — typically ``{tray_info_idx, tray_color}`` — and omits the
        ~18 other keys (tray_type, state, k, n, cali_idx, nozzle_temp_min/max,
        tray_uuid, xcam_info, ...) the slicer needs to render the slot.
        Before this fix the per-field accumulate replaced the cached vt_tray
        wholesale (it only carried over prev keys NOT present in new), so the
        next 1 Hz cached-as-base push handed the slicer a stripped vt_tray and
        BambuStudio rendered the external slot as "invalid" until a reload
        triggered a fresh pushall. AMS slots didn't suffer because
        ``_merge_ams_dict`` already deep-merged them. The fix overlays incoming
        keys onto the previous dict for every top-level dict-shaped field
        (excluding ams, which keeps its own deep merge).
        """
        server = _make_server()
        bridge = _make_bridge(server)
        await bridge.start()

        # 1. Pushall response with the full ~20-field vt_tray dict a real
        # P1S sends to bootstrap the slot.
        full_push = json.dumps(
            {
                "print": {
                    "command": "push_status",
                    "vt_tray": {
                        "id": "254",
                        "tray_info_idx": "Pea5f68f",
                        "tray_type": "PLA",
                        "tray_sub_brands": "",
                        "tray_color": "F72323FF",
                        "tray_weight": "0",
                        "tray_diameter": "0.00",
                        "tray_temp": "0",
                        "tray_time": "0",
                        "bed_temp_type": "0",
                        "bed_temp": "0",
                        "nozzle_temp_max": "240",
                        "nozzle_temp_min": "190",
                        "xcam_info": "000000000000000000000000",
                        "tray_uuid": "00000000000000000000000000000000",
                        "ctype": 0,
                        "remain": -1,
                        "k": 0.01999999955296,
                        "n": 1,
                        "cali_idx": -1,
                        "state": 3,
                    },
                }
            }
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", full_push)
        await asyncio.sleep(0.01)

        # 2. Incremental push carrying just the two fields the slicer's pick
        # changed — exactly the shape the P1S firmware sends after an
        # ams_filament_setting ack.
        incremental_push = json.dumps(
            {
                "print": {
                    "command": "push_status",
                    "vt_tray": {
                        "tray_info_idx": "Pea5f68f",
                        "tray_color": "76D9F4FF",
                    },
                }
            }
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", incremental_push)
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        vt = cached["vt_tray"]
        # Incoming fields applied.
        assert vt["tray_info_idx"] == "Pea5f68f"
        assert vt["tray_color"] == "76D9F4FF"
        # All other fields preserved from the prior pushall — without these
        # the slicer rendered the slot as invalid.
        assert vt["tray_type"] == "PLA"
        assert vt["state"] == 3
        assert vt["remain"] == -1
        assert vt["k"] == 0.01999999955296
        assert vt["n"] == 1
        assert vt["cali_idx"] == -1
        assert vt["nozzle_temp_min"] == "190"
        assert vt["nozzle_temp_max"] == "240"
        assert vt["tray_uuid"] == "00000000000000000000000000000000"
        assert vt["id"] == "254"

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_tray_exist_bits_clears_empty_slots_in_slicer_cache(self):
        """Upstream Bambuddy #1726: the bridge cache forwards the real
        printer's raw AMS payload to the slicer. Without the empty-slot
        cleanup that bambu_mqtt.py applies to BamDude's internal state, the
        cached units carried stale ``tray_type`` / ``tray_color`` /
        ``tray_info_idx`` for slots whose ``tray_exist_bits`` bit was 0 — and
        BambuStudio's Sync rendered those empty slots as phantom loaded
        filaments. After the fix the bridge runs the same shared
        ``apply_tray_exist_bits`` helper before storing the cache.
        """
        server = _make_server()
        bridge = _make_bridge(server)
        await bridge.start()

        # Pushall: AMS 0 has slots 0/1/2/3; only slots 1, 2, 3 are loaded.
        # Slot 0 carries stale data (RFID/color/material from a previously
        # loaded spool). ``tray_exist_bits`` = 0xe = 0b1110 → bit 0 unset.
        bridge._on_printer_raw(
            f"device/{H2D_SERIAL}/report",
            json.dumps(
                {
                    "print": {
                        "command": "push_status",
                        "ams": {
                            "ams": [
                                {
                                    "id": "0",
                                    "tray": [
                                        {
                                            "id": "0",
                                            "tray_type": "PLA",
                                            "tray_color": "FF0000FF",
                                            "tray_info_idx": "GFL00",
                                            "tag_uid": "1234567890abcdef",
                                            "tray_uuid": "abcdef1234567890abcdef1234567890",
                                            "remain": 75,
                                            "state": "11",
                                        },
                                        {"id": "1", "tray_type": "PETG", "tray_color": "00FF00FF"},
                                        {"id": "2", "tray_type": "ABS", "tray_color": "0000FFFF"},
                                        {"id": "3", "tray_type": "TPU", "tray_color": "FFFF00FF"},
                                    ],
                                }
                            ],
                            "tray_exist_bits": "e",
                        },
                    }
                }
            ).encode(),
        )
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        slot0 = cached["ams"]["ams"][0]["tray"][0]
        # Empty slot: stale per-tray fields wiped, state promoted to 9.
        assert slot0["state"] == 9, "empty slot must be promoted to state=9"
        assert slot0["tray_type"] == ""
        assert slot0["tray_color"] == ""
        assert slot0["tray_info_idx"] == ""
        assert slot0["tag_uid"] == "0000000000000000"
        assert slot0["tray_uuid"] == "00000000000000000000000000000000"
        assert slot0["remain"] == 0
        # Loaded slots preserved.
        assert cached["ams"]["ams"][0]["tray"][1]["tray_type"] == "PETG"
        assert cached["ams"]["ams"][0]["tray"][2]["tray_type"] == "ABS"
        assert cached["ams"]["ams"][0]["tray"][3]["tray_type"] == "TPU"

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_tray_exist_bits_shutdown_guard_preserves_cache(self):
        """#765 shutdown guard mirrored at the bridge: when the printer
        powers off it sends all-zero ``tray_exist_bits`` paired with
        ``power_on_flag=False``. Wiping the cache on that pattern would
        propagate phantom empties to every slicer reconnect until the
        printer powers back on and pushes a real state. Skip cleanup
        on the shutdown-shaped payload."""
        server = _make_server()
        bridge = _make_bridge(server)
        await bridge.start()

        # 1. Normal pushall — all four slots loaded.
        bridge._on_printer_raw(
            f"device/{H2D_SERIAL}/report",
            json.dumps(
                {
                    "print": {
                        "command": "push_status",
                        "ams": {
                            "ams": [
                                {
                                    "id": "0",
                                    "tray": [
                                        {"id": str(i), "tray_type": "PLA", "tray_color": f"{i:02x}{i:02x}{i:02x}FF"}
                                        for i in range(4)
                                    ],
                                }
                            ],
                            "tray_exist_bits": "f",
                            "power_on_flag": True,
                        },
                    }
                }
            ).encode(),
        )
        await asyncio.sleep(0.01)

        # 2. Shutdown-shaped push: tray_exist_bits=0 + power_on_flag=False.
        bridge._on_printer_raw(
            f"device/{H2D_SERIAL}/report",
            json.dumps(
                {
                    "print": {
                        "command": "push_status",
                        "ams": {
                            "tray_exist_bits": "0",
                            "power_on_flag": False,
                        },
                    }
                }
            ).encode(),
        )
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        for i in range(4):
            assert cached["ams"]["ams"][0]["tray"][i]["tray_type"] == "PLA", f"slot {i} must survive the shutdown push"

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_tray_exist_bits_skips_ams_ht_units(self):
        """AMS-HT units (id >= 128) use a separate addressing scheme and
        must not be touched by the bitmask cleanup — bit math at
        global_bit = ams_id * 4 + tray_id would overrun normal AMS bits.
        Pin the skip so future AMS-HT support doesn't accidentally wipe
        loaded HT slots.
        """
        server = _make_server()
        bridge = _make_bridge(server)
        await bridge.start()

        bridge._on_printer_raw(
            f"device/{H2D_SERIAL}/report",
            json.dumps(
                {
                    "print": {
                        "command": "push_status",
                        "ams": {
                            "ams": [
                                {
                                    "id": "128",
                                    "tray": [
                                        {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF"},
                                    ],
                                }
                            ],
                            "tray_exist_bits": "0",
                            "power_on_flag": True,
                        },
                    }
                }
            ).encode(),
        )
        await asyncio.sleep(0.01)

        cached = bridge.get_latest_print_state()
        ht_slot = cached["ams"]["ams"][0]["tray"][0]
        # tray_exist_bits="0" alone would normally wipe — but AMS-HT is
        # skipped, so the HT slot keeps its loaded data.
        assert ht_slot["tray_type"] == "PLA"

        await bridge.stop()


# ---------------------------------------------------------------------------
# Caching: get_version response
# ---------------------------------------------------------------------------


class TestVersionCache:
    @pytest.mark.asyncio
    async def test_get_version_response_caches_modules(self):
        server = _make_server()
        bridge = _make_bridge(server)
        await bridge.start()

        payload = json.dumps(
            {
                "info": {
                    "command": "get_version",
                    "module": [
                        {"name": "ota", "sn": H2D_SERIAL, "sw_ver": "01.03.00.00"},
                        {"name": "n3f/0", "sn": "AMS_HW_1", "sw_ver": "04.00.21.87"},
                    ],
                }
            }
        ).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", payload)
        await asyncio.sleep(0.01)

        modules = bridge.get_latest_version_modules()
        assert modules is not None
        assert len(modules) == 2
        # Device-level sn rewritten; AMS-hardware sn left alone.
        assert modules[0]["sn"] == VP_SERIAL
        assert modules[1]["sn"] == "AMS_HW_1"

        await bridge.stop()


# ---------------------------------------------------------------------------
# Selective fan-out (everything that's not push_status / get_version)
# ---------------------------------------------------------------------------


class TestCommandResponseFanout:
    @pytest.mark.asyncio
    async def test_extrusion_cali_get_response_is_fanned_out(self):
        """Slicer's extrusion_cali_get goes to the printer; the printer's response
        must reach the slicer or BambuStudio's pre-flight blocks Send."""
        server = _make_server()
        server.push_raw_to_clients = AsyncMock()
        bridge = _make_bridge(server)
        await bridge.start()

        body = json.dumps({"print": {"command": "extrusion_cali_get", "filaments": []}}).encode()
        bridge._on_printer_raw(f"device/{H2D_SERIAL}/report", body)
        await asyncio.sleep(0.01)

        server.push_raw_to_clients.assert_awaited_once()
        topic, _payload = server.push_raw_to_clients.await_args.args
        assert topic == f"device/{VP_SERIAL}/report"

        await bridge.stop()


# ---------------------------------------------------------------------------
# Forwarding: slicer → printer
# ---------------------------------------------------------------------------


class TestForwardToPrinter:
    @pytest.mark.asyncio
    async def test_forward_publishes_to_real_serial_request_topic(self):
        target = _make_paho_client()
        bridge = _make_bridge(_make_server(), target)
        await bridge.start()

        ok = bridge.forward_to_printer({"print": {"command": "stop"}})
        assert ok is True
        target.publish_raw.assert_called_once()
        topic, payload = target.publish_raw.call_args.args
        assert topic == f"device/{H2D_SERIAL}/request"
        assert json.loads(payload) == {"print": {"command": "stop"}}

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_forward_returns_false_when_not_bound(self):
        pm = MagicMock()
        pm.get_client = MagicMock(return_value=None)
        bridge = MQTTBridge(
            vp_id=1,
            vp_name="vp1",
            vp_serial=VP_SERIAL,
            target_printer_id=42,
            mqtt_server=_make_server(),
            printer_manager=pm,
        )
        await bridge.start()
        assert bridge.forward_to_printer({"print": {"command": "stop"}}) is False
        await bridge.stop()


# ---------------------------------------------------------------------------
# SimpleMQTTServer status response: cached-as-base
# ---------------------------------------------------------------------------


class TestStatusReportCachedAsBase:
    """`_send_status_report` sends near-byte-identical real data when bridge cache exists."""

    def _capture_published(self, server: SimpleMQTTServer):
        """Wrap _publish_to_report to capture (topic, payload_dict)."""
        published: list = []

        async def _capture(writer, payload, serial=""):
            published.append((serial or server.serial, payload))

        server._publish_to_report = _capture  # type: ignore[assignment]
        return published

    @pytest.mark.asyncio
    async def test_uses_real_cache_when_bridge_active(self):
        server = _make_server()
        bridge = MagicMock()
        bridge.get_latest_print_state.return_value = {
            "command": "push_status",
            "msg": 0,
            "ams": {"ams": [{"id": "0"}]},
            "device": {"extruder": {"info": [{"id": 0}, {"id": 1}]}},
            "nozzle_diameter": "0.4",
            "nozzle_type": "HH01",  # real H2D value, not synthetic 'hardened_steel'
        }
        server.set_bridge(bridge)
        published = self._capture_published(server)

        await server._send_status_report(MagicMock())
        assert len(published) == 1
        _serial, payload = published[0]
        # AMS / device / nozzle_type all from cache
        assert payload["print"]["nozzle_type"] == "HH01"
        assert payload["print"]["device"]["extruder"]["info"][1]["id"] == 1
        # Protocol fields under our control
        assert payload["print"]["command"] == "push_status"
        assert payload["print"]["gcode_state"] == "IDLE"

    @pytest.mark.asyncio
    async def test_falls_back_to_synthetic_when_no_cache(self):
        server = _make_server()
        bridge = MagicMock()
        bridge.get_latest_print_state.return_value = None
        server.set_bridge(bridge)
        published = self._capture_published(server)

        await server._send_status_report(MagicMock())
        assert len(published) == 1
        _serial, payload = published[0]
        # Synthetic baseline has stub fields like nozzle_type='hardened_steel'
        # and a `storage` field that the real H2D doesn't push.
        assert payload["print"]["nozzle_type"] == "hardened_steel"
        assert "storage" in payload["print"]

    @pytest.mark.asyncio
    async def test_overrides_protocol_fields_even_when_cache_present(self):
        """Cached value's gcode_state must NOT win over our local upload-state-machine value."""
        server = _make_server()
        server._gcode_state = "PREPARE"
        server._current_file = "foo.3mf"
        server.upload_started()  # mid-FTP-upload, so PREPARE is the live state (not stale)
        bridge = MagicMock()
        bridge.get_latest_print_state.return_value = {
            "command": "push_status",
            "gcode_state": "IDLE",  # printer is idle; we are mid-FTP-upload
            "gcode_file": "",
            "gcode_file_prepare_percent": "0",
        }
        server.set_bridge(bridge)
        published = self._capture_published(server)

        await server._send_status_report(MagicMock())
        _serial, payload = published[0]
        assert payload["print"]["gcode_state"] == "PREPARE"
        assert payload["print"]["gcode_file"] == "foo.3mf"

    @staticmethod
    def _mid_print_cache() -> dict:
        return {
            "command": "push_status",
            "gcode_state": "RUNNING",  # real printer is mid-print
            "gcode_file": "on-the-bed.gcode.3mf",
            "subtask_name": "on-the-bed",
            "mc_print_stage": "2",
            "mc_percent": 47,
            "mc_remaining_time": 1234,
            "stg": [1, 2],
            "stg_cur": 3,
            "layer_num": 88,
            "total_layer_num": 200,
            "print_error": 5,
        }

    @pytest.mark.asyncio
    async def test_activity_fields_mirrored_when_printer_mid_print(self):
        """#1887: with the printer mid-print and no upload handshake of ours in
        flight, the report carries the printer's real progress under a forced
        FINISH — the one state that renders the slicer's progress panel without
        tripping ``is_in_printing()`` and disabling Send."""
        server = _make_server()
        bridge = MagicMock()
        bridge.get_latest_print_state.return_value = self._mid_print_cache()
        server.set_bridge(bridge)
        published = self._capture_published(server)

        await server._send_status_report(MagicMock())
        _serial, payload = published[0]
        pb = payload["print"]
        assert pb["gcode_state"] == "FINISH"  # not RUNNING — Send stays enabled
        assert pb["gcode_file"] == "on-the-bed.gcode.3mf"
        assert pb["subtask_name"] == "on-the-bed"
        assert pb["gcode_file_prepare_percent"] == "100"
        assert pb["mc_print_stage"] == "2"
        assert pb["mc_percent"] == 47
        assert pb["mc_remaining_time"] == 1234
        assert pb["stg"] == [1, 2]
        assert pb["stg_cur"] == 3
        assert pb["layer_num"] == 88
        assert pb["total_layer_num"] == 200
        # Never mirrored: the VP isn't the machine that threw the fault, and a
        # non-zero code raises a modal error dialog in StatusPanel.
        assert pb["print_error"] == 0

    @pytest.mark.asyncio
    async def test_activity_fields_zeroed_when_printer_idle(self):
        """#1558: with nothing printing, forcing gcode_state=IDLE isn't enough —
        Send pre-flight also reads mc_percent / stg_cur / layer_num / …, and a
        report that says IDLE while they are non-zero is read as "busy"."""
        server = _make_server()
        bridge = MagicMock()
        cache = self._mid_print_cache()
        cache["gcode_state"] = "IDLE"  # printer between jobs
        bridge.get_latest_print_state.return_value = cache
        server.set_bridge(bridge)
        published = self._capture_published(server)

        await server._send_status_report(MagicMock())
        _serial, payload = published[0]
        pb = payload["print"]
        assert pb["gcode_state"] == "IDLE"
        assert pb["mc_print_stage"] == ""
        assert pb["mc_percent"] == 0
        assert pb["mc_remaining_time"] == 0
        assert pb["stg"] == []
        assert pb["stg_cur"] == 0
        assert pb["layer_num"] == 0
        assert pb["total_layer_num"] == 0
        assert pb["print_error"] == 0

    @pytest.mark.asyncio
    async def test_mirror_suppressed_while_our_upload_handshake_settles(self):
        """The slicer releases its in-flight-job lock only once it sees FINISH
        carrying the subtask_name it just uploaded (#1280 / #1658). Swapping in
        the printer's filename during that window wedges the send modal, so a
        fresh state transition holds the mirror off."""
        server = _make_server()
        bridge = MagicMock()
        bridge.get_latest_print_state.return_value = self._mid_print_cache()
        server.set_bridge(bridge)
        published = self._capture_published(server)

        server.set_gcode_state("FINISH", filename="ours.3mf", prepare_percent="100")
        await server._send_status_report(MagicMock())
        _serial, payload = published[0]
        pb = payload["print"]
        assert pb["gcode_file"] == "ours.3mf"  # our filename, not the printer's
        assert pb["subtask_name"] == "ours"
        assert pb["mc_percent"] == 0  # progress still zeroed

    @pytest.mark.asyncio
    async def test_mirror_suppressed_during_prepare(self):
        """While our own job is being handed over, the VP's upload state machine
        owns the report."""
        server = _make_server()
        bridge = MagicMock()
        bridge.get_latest_print_state.return_value = self._mid_print_cache()
        server.set_bridge(bridge)
        published = self._capture_published(server)

        server._gcode_state = "PREPARE"
        server._current_file = "ours.3mf"
        server.upload_started()  # mid-FTP, so PREPARE is live rather than stale
        server._state_changed_at = float("-inf")  # settle window already elapsed
        await server._send_status_report(MagicMock())
        _serial, payload = published[0]
        pb = payload["print"]
        assert pb["gcode_state"] == "PREPARE"
        assert pb["mc_percent"] == 0

    @pytest.mark.asyncio
    async def test_mirror_survives_an_abandoned_send(self):
        """BamDude divergence: a ``project_file`` whose upload never arrives
        leaves the raw ``_gcode_state`` on PREPARE forever. The mirror keys off
        the EFFECTIVE state, so the stale-PREPARE downgrade to IDLE re-enables
        it instead of killing it permanently."""
        server = _make_server()
        bridge = MagicMock()
        bridge.get_latest_print_state.return_value = self._mid_print_cache()
        server.set_bridge(bridge)
        published = self._capture_published(server)

        server._gcode_state = "PREPARE"  # abandoned Send: no upload ever started
        server._prepare_set_monotonic = 0.0  # grace window long gone
        server._state_changed_at = float("-inf")
        await server._send_status_report(MagicMock())
        _serial, payload = published[0]
        assert payload["print"]["gcode_state"] == "FINISH"
        assert payload["print"]["mc_percent"] == 47

    @pytest.mark.asyncio
    async def test_storage_indicators_overlaid_for_send_preflight(self):
        """When the cached push lacks SD/storage indicators (P1S/A1 with no SD card,
        older field shapes), the cached-as-base path must overlay them so
        BambuStudio's Send pre-flight reads them as "storage available" — slicer
        FTPs to BamDude, not the printer's SD card. Pin upstream #1228 fix.
        """
        server = _make_server()
        bridge = MagicMock()
        # Real-printer-shaped push that doesn't report SD/storage (P1S/A1
        # firmware that hit #1228 — partial home_flag, no sdcard field, no
        # storage block).
        bridge.get_latest_print_state.return_value = {
            "command": "push_status",
            "msg": 0,
            "home_flag": 0x10,  # partial bits, NO 0x100 (HAS_SDCARD_NORMAL)
            "sdcard": False,
        }
        server.set_bridge(bridge)
        published = self._capture_published(server)

        await server._send_status_report(MagicMock())
        _serial, payload = published[0]
        # Bit 0x100 OR'd onto partial home_flag (preserves the 0x10 bits).
        assert payload["print"]["home_flag"] == 0x10 | 0x100
        # sdcard forced True even when real reported False.
        assert payload["print"]["sdcard"] is True
        # storage block injected when cache lacks one; non-zero free/total.
        assert "storage" in payload["print"]
        assert payload["print"]["storage"]["free"] > 0
        assert payload["print"]["storage"]["total"] > 0

    @pytest.mark.asyncio
    async def test_storage_indicators_preserve_real_storage_when_present(self):
        """When the real printer DOES report SD/storage (H2D, X1C), the overlay
        must not clobber it — setdefault preserves the real storage block, and
        OR with 0x100 on home_flag is idempotent when the bit is already set.
        """
        server = _make_server()
        bridge = MagicMock()
        bridge.get_latest_print_state.return_value = {
            "command": "push_status",
            "home_flag": 0x100 | 0x42,  # bit already set + other bits
            "sdcard": True,
            "storage": {"free": 12345, "total": 99999},
        }
        server.set_bridge(bridge)
        published = self._capture_published(server)

        await server._send_status_report(MagicMock())
        _serial, payload = published[0]
        # Real home_flag bits all preserved (0x100 | 0x42 idempotent under | 0x100).
        assert payload["print"]["home_flag"] == 0x100 | 0x42
        # Real storage block passed through unchanged — overlay never overrides
        # what the printer actually reported.
        assert payload["print"]["storage"] == {"free": 12345, "total": 99999}


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestWireFormat:
    """BambuStudio's Send pre-flight rejects compact JSON — must match real printer's
    indented format (32K bytes for an idle H2D vs 14K compact)."""

    @pytest.mark.asyncio
    async def test_publish_uses_indent_4_json_format(self):
        server = _make_server()
        captured: list = []

        writer = MagicMock()
        writer.write = lambda data: captured.append(data)
        writer.drain = AsyncMock()

        await server._publish_to_report(writer, {"print": {"command": "push_status", "ams": {}}})

        body = b"".join(captured)
        assert b'\n    "print"' in body, "publish_to_report must use indent=4 JSON"


# ---------------------------------------------------------------------------
# Routing: _handle_publish
# ---------------------------------------------------------------------------


class TestPublishRouting:
    """Slicer-issued commands: project_file/gcode_file handled locally, everything
    else forwarded to the real printer."""

    def _build_publish_payload(self, topic: str, body: bytes) -> bytes:
        topic_bytes = topic.encode("utf-8")
        return bytes([len(topic_bytes) >> 8, len(topic_bytes) & 0xFF]) + topic_bytes + body

    def _attach_active_bridge(self, server: SimpleMQTTServer) -> MagicMock:
        bridge = MagicMock()
        bridge.is_active = True
        bridge.forward_to_printer = MagicMock(return_value=True)
        server.set_bridge(bridge)
        return bridge

    @pytest.mark.asyncio
    async def test_project_file_handled_locally_not_forwarded(self):
        server = _make_server()
        bridge = self._attach_active_bridge(server)
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        body = json.dumps({"print": {"command": "project_file", "subtask_name": "f", "sequence_id": "1"}}).encode()
        payload = self._build_publish_payload(f"device/{VP_SERIAL}/request", body)

        with patch.object(server, "_send_print_response", new=AsyncMock()) as mock_resp:
            await server._handle_publish(0x30, payload, writer, "client1")

        bridge.forward_to_printer.assert_not_called()
        mock_resp.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gcode_file_handled_locally_not_forwarded(self):
        server = _make_server()
        bridge = self._attach_active_bridge(server)
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        body = json.dumps({"print": {"command": "gcode_file", "subtask_name": "f.gcode", "sequence_id": "1"}}).encode()
        payload = self._build_publish_payload(f"device/{VP_SERIAL}/request", body)

        with patch.object(server, "_send_print_response", new=AsyncMock()):
            await server._handle_publish(0x30, payload, writer, "client1")

        bridge.forward_to_printer.assert_not_called()

    @pytest.mark.asyncio
    async def test_pushall_handled_locally_not_forwarded(self):
        server = _make_server()
        bridge = self._attach_active_bridge(server)
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        body = json.dumps({"pushing": {"command": "pushall", "sequence_id": "0"}}).encode()
        payload = self._build_publish_payload(f"device/{VP_SERIAL}/request", body)

        with patch.object(server, "_send_status_report", new=AsyncMock()) as mock_status:
            await server._handle_publish(0x30, payload, writer, "client1")

        # Synthetic answer fires (fast, low latency); no forwarding (the
        # cache already mirrors what the printer would respond with).
        bridge.forward_to_printer.assert_not_called()
        mock_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_version_handled_locally_not_forwarded(self):
        server = _make_server()
        bridge = self._attach_active_bridge(server)
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        body = json.dumps({"info": {"command": "get_version", "sequence_id": "1"}}).encode()
        payload = self._build_publish_payload(f"device/{VP_SERIAL}/request", body)

        with patch.object(server, "_send_version_response", new=AsyncMock()) as mock_ver:
            await server._handle_publish(0x30, payload, writer, "client1")

        bridge.forward_to_printer.assert_not_called()
        mock_ver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extrusion_cali_get_is_forwarded(self):
        """extrusion_cali_get fetches per-filament k-profiles — must reach the printer."""
        server = _make_server()
        bridge = self._attach_active_bridge(server)
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        body = json.dumps(
            {
                "print": {
                    "command": "extrusion_cali_get",
                    "filament_id": "",
                    "nozzle_diameter": "0.4",
                    "sequence_id": "5",
                }
            }
        ).encode()
        payload = self._build_publish_payload(f"device/{VP_SERIAL}/request", body)

        await server._handle_publish(0x30, payload, writer, "client1")

        bridge.forward_to_printer.assert_called_once()
        forwarded = bridge.forward_to_printer.call_args.args[0]
        assert forwarded["print"]["command"] == "extrusion_cali_get"

    @pytest.mark.asyncio
    async def test_print_stop_is_forwarded(self):
        server = _make_server()
        bridge = self._attach_active_bridge(server)
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()

        body = json.dumps({"print": {"command": "stop", "sequence_id": "5"}}).encode()
        payload = self._build_publish_payload(f"device/{VP_SERIAL}/request", body)

        await server._handle_publish(0x30, payload, writer, "client1")

        bridge.forward_to_printer.assert_called_once()


# ---------------------------------------------------------------------------
# IP encoding helper
# ---------------------------------------------------------------------------


class TestIpEncoding:
    def test_le_uint32_matches_real_h2d_capture(self):
        # 192.168.255.133 captured from real H2D's net.info[0].ip = 2248124608
        assert _ip_to_uint32_le("192.168.255.133") == 2248124608

    def test_vp_ip_round_trip(self):
        assert _ip_to_uint32_le("192.168.255.16") == 285190336

    def test_invalid_ip_raises(self):
        with pytest.raises(ValueError):
            _ip_to_uint32_le("not.an.ip.actually")


# ---------------------------------------------------------------------------
# Hostname/FQDN target resolution (#1429 follow-up, C6)
# ---------------------------------------------------------------------------


class TestHostnameResolution:
    """Users who configured the printer by FQDN (common on LANs with router-
    provided DNS like ``p1s.fritz.box``) hit ``invalid IPv4`` on the encoder and
    the rewrite never armed — the slicer kept FTPing direct to the real printer.
    The bridge now resolves hostname → IPv4 first."""

    def test_pass_through_for_valid_ipv4(self):
        assert _resolve_target_to_ipv4("192.168.1.50") == "192.168.1.50"

    def test_empty_returns_none(self):
        assert _resolve_target_to_ipv4("") is None
        assert _resolve_target_to_ipv4(None) is None  # type: ignore[arg-type]

    def test_hostname_resolves_via_getaddrinfo(self):
        with patch(
            "backend.app.services.virtual_printer.mqtt_bridge.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.168.3.153", 0))],
        ) as mock_gai:
            assert _resolve_target_to_ipv4("p1s.fritz.box") == "192.168.3.153"
        # AF_INET filter prevents an IPv6-only result from being picked, since
        # net.info[*].ip is a uint32 LE that can't carry v6.
        assert mock_gai.call_args.kwargs.get("family") == socket.AF_INET

    def test_dns_failure_returns_none(self):
        with patch(
            "backend.app.services.virtual_printer.mqtt_bridge.socket.getaddrinfo",
            side_effect=OSError("Name or service not known"),
        ):
            assert _resolve_target_to_ipv4("nope.invalid") is None

    @pytest.mark.asyncio
    async def test_fqdn_target_arms_encoding(self, caplog):
        """A hostname-configured printer must still arm the net.info rewrite:
        resolve → encode, and the armed log carries configured->resolved."""
        server = _make_server(bind_address=VP_IP)
        target = _make_paho_client(ip="p1s.fritz.box")
        bridge = _make_bridge(server, target=target)

        with (
            patch(
                "backend.app.services.virtual_printer.mqtt_bridge.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", (H2D_IP, 0))],
            ),
            caplog.at_level(logging.INFO, logger="backend.app.services.virtual_printer.mqtt_bridge"),
        ):
            await bridge.start()

        assert bridge._target_ip_uint32_le == _ip_to_uint32_le(H2D_IP)
        assert bridge._vp_ip_uint32_le == _ip_to_uint32_le(VP_IP)
        armed = [r for r in caplog.records if "encoding armed" in r.getMessage()]
        assert len(armed) == 1
        assert f"p1s.fritz.box->{H2D_IP}" in armed[0].getMessage()
        await bridge.stop()


# ---------------------------------------------------------------------------
# Not-armed diagnostic logging (#1429 defensive, C7)
# ---------------------------------------------------------------------------


class TestNotArmedDiagnosticLogging:
    """Each path that leaves the net.info rewrite unarmed now names its reason
    (deduped to one line per state change), so a silent no-op is visible in the
    logs instead of just an absent "armed" line."""

    @pytest.mark.asyncio
    async def test_missing_target_ip_logs_specific_reason(self, caplog):
        server = _make_server(bind_address=VP_IP)
        target = _make_paho_client(ip="")
        bridge = _make_bridge(server, target=target)

        with caplog.at_level(logging.INFO, logger="backend.app.services.virtual_printer.mqtt_bridge"):
            await bridge.start()

        not_armed = [r for r in caplog.records if "NOT armed" in r.getMessage()]
        assert len(not_armed) == 1
        assert "no ip_address" in not_armed[0].getMessage()
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_unresolvable_hostname_logs_configured_value(self, caplog):
        server = _make_server(bind_address=VP_IP)
        target = _make_paho_client(ip="nope.invalid")
        bridge = _make_bridge(server, target=target)

        with (
            patch(
                "backend.app.services.virtual_printer.mqtt_bridge.socket.getaddrinfo",
                side_effect=OSError("Name or service not known"),
            ),
            caplog.at_level(logging.INFO, logger="backend.app.services.virtual_printer.mqtt_bridge"),
        ):
            await bridge.start()

        assert bridge._target_ip_uint32_le is None
        not_armed = [r for r in caplog.records if "NOT armed" in r.getMessage()]
        assert len(not_armed) == 1
        # Names the configured value, distinguishing "DNS gave v6" from "garbage".
        assert "nope.invalid" in not_armed[0].getMessage()
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_no_matching_host_interface_logs_reason(self, caplog):
        server = _make_server(bind_address="0.0.0.0")
        bridge = _make_bridge(server)

        with (
            patch(
                "backend.app.services.virtual_printer.mqtt_bridge._resolve_host_interface_for_target",
                return_value=None,
            ),
            caplog.at_level(logging.INFO, logger="backend.app.services.virtual_printer.mqtt_bridge"),
        ):
            await bridge.start()

        not_armed = [r for r in caplog.records if "NOT armed" in r.getMessage()]
        assert len(not_armed) == 1
        msg = not_armed[0].getMessage()
        assert H2D_IP in msg
        assert "no host interface" in msg
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_reason_cleared_on_arm_then_reemits(self, caplog):
        """Dedup must reset when arming succeeds, so a later regression re-logs."""
        server = _make_server(bind_address=VP_IP)
        bridge = _make_bridge(server)  # dotted-quad target → arms immediately
        await bridge.start()
        assert bridge._not_armed_reason is None
        await bridge.stop()
