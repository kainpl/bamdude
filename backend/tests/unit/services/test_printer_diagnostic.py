"""Unit tests for the connection diagnostic.

Pins the pass / fail / warn / skip contract of each check. Those statuses
drive the localized fix text the user sees when a printer won't connect,
so a status flip is a user-facing regression — each one is asserted here.
"""

import ipaddress
import subprocess
import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.services.printer_diagnostic import (
    _host_source_ip,
    _interpreter_is_signed,
    _same_subnet,
    run_connection_diagnostic,
)

MOD = "backend.app.services.printer_diagnostic"


def _statuses(result):
    """Map of check id -> status for concise assertions."""
    return {c.id: c.status for c in result.checks}


def _check(result, check_id):
    """The one check with this id — for asserting on its params, not just status."""
    return next(c for c in result.checks if c.id == check_id)


def _port_probe(overrides=None):
    """Sync side_effect for _check_port. Defaults: every port reachable."""
    reachable = {8883: True, 990: True, 322: True, 6000: True}
    reachable.update(overrides or {})

    def _probe(ip, port, timeout=3.0):
        return reachable[port]

    return _probe


def _state(*, connected=True, developer_mode=True, store_to_sdcard=True):
    return types.SimpleNamespace(connected=connected, developer_mode=developer_mode, store_to_sdcard=store_to_sdcard)


class _Env:
    """Patches the diagnostic's network/printer helpers for one run."""

    def __init__(
        self,
        *,
        ports=None,
        runtime="Docker",
        network_mode="host",
        host_ip="192.168.1.5",
        host_subnet="192.168.1.0/24",
        state=None,
        test_connection_success=True,
        report_messages_since_connect: int | None = 5,
        ftps_handshake: str = "ok",
        platform="linux",
    ):
        self.ports = ports or _port_probe()
        # What port 990 answers once it accepts a TCP connection: "ok" (TLS)
        # or "no_tls" (audit 91acac2b — an open port is not a working one).
        self.ftps_handshake = ftps_handshake
        # Container engine detect_container_runtime() reports, None for bare metal.
        self.runtime = runtime
        self.network_mode = network_mode
        self.host_ip = host_ip
        # The prefix the host's own interface carries; None means no
        # interface claims host_ip.
        self.host_subnet = host_subnet
        # Pinned so the check list does not depend on the OS the suite runs
        # on: macos_local_network is emitted on darwin only (upstream #3114).
        self.platform = platform
        self.state = state
        self.test_connection_success = test_connection_success
        # ``None`` means get_client returns None (e.g. pre-add flow); an int is
        # the client's report_messages_since_connect counter for the
        # printer_publishing check (#1622).
        self.report_messages_since_connect = report_messages_since_connect
        self._stack = ExitStack()

    def __enter__(self):
        manager = MagicMock()
        manager.get_status.return_value = self.state
        manager.test_connection = AsyncMock(return_value={"success": self.test_connection_success})
        # Exposed so a test can assert the probe was NOT made. Opening a second
        # MQTT session to a printer that is already connected is a real cost,
        # not an implementation detail, so it is asserted directly.
        self.test_connection = manager.test_connection
        if self.report_messages_since_connect is None:
            manager.get_client.return_value = None
        else:
            client = MagicMock()
            client.report_messages_since_connect = self.report_messages_since_connect
            manager.get_client.return_value = client
        self._stack.enter_context(patch(f"{MOD}._check_port", new_callable=AsyncMock, side_effect=self.ports))
        self._stack.enter_context(
            patch(f"{MOD}._ftps_handshake", new_callable=AsyncMock, return_value=self.ftps_handshake)
        )
        self._stack.enter_context(patch(f"{MOD}.detect_container_runtime", return_value=self.runtime))
        self._stack.enter_context(patch(f"{MOD}._detect_container_network_mode", return_value=self.network_mode))
        self._stack.enter_context(patch(f"{MOD}._host_source_ip", return_value=self.host_ip))
        self._stack.enter_context(
            patch(
                f"{MOD}.find_local_ipv4_network",
                return_value=ipaddress.ip_network(self.host_subnet) if self.host_subnet else None,
            )
        )
        self._stack.enter_context(patch(f"{MOD}.printer_manager", manager))
        self._stack.enter_context(patch(f"{MOD}.sys.platform", self.platform))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False


def _printer(ip="192.168.1.50", model=None):
    return types.SimpleNamespace(id=1, ip_address=ip, model=model)


def _with_host_network(subnet: str | None):
    """Patch the host's own interface prefix, the way the kernel reports it."""
    return patch(f"{MOD}.find_local_ipv4_network", return_value=ipaddress.ip_network(subnet) if subnet else None)


class TestSameSubnet:
    def test_same_24(self):
        with _with_host_network("192.168.1.0/24"):
            assert _same_subnet("192.168.1.10", "192.168.1.200") is True

    def test_different_24(self):
        with _with_host_network("192.168.2.0/24"):
            assert _same_subnet("192.168.1.10", "192.168.2.10") is False

    def test_a_22_reaches_across_the_third_octet(self):
        """upstream #3092: 192.168.96.9/22 and 192.168.98.170 are one LAN; the
        old /24 guess called them different networks."""
        with _with_host_network("192.168.96.0/22"):
            assert _same_subnet("192.168.98.170", "192.168.96.9") is True

    def test_a_25_does_not_reach_the_whole_24(self):
        with _with_host_network("192.168.1.0/25"):
            assert _same_subnet("192.168.1.200", "192.168.1.10") is False

    def test_no_interface_claims_the_host_address(self):
        """Undeterminable stays undeterminable — it must not become a warning."""
        with _with_host_network(None):
            assert _same_subnet("192.168.1.10", "192.168.1.200") is None

    def test_hostname_undeterminable(self):
        assert _same_subnet("printer.local", "192.168.1.10") is None

    def test_ipv6_undeterminable(self):
        assert _same_subnet("fe80::1", "192.168.1.10") is None


class TestHostSourceIp:
    """upstream #3092: which of BamDude's own addresses the printer is compared with."""

    def test_the_route_is_probed_toward_the_printer(self):
        # Not toward a fixed far-away address: on a multi-homed host the source
        # for a route to the internet is not the interface the printer is on.
        sock = MagicMock()
        sock.getsockname.return_value = ("192.168.96.9", 51234)
        with patch(f"{MOD}.socket.socket", return_value=sock):
            assert _host_source_ip("192.168.98.170") == "192.168.96.9"
        sock.connect.assert_called_once_with(("192.168.98.170", 1))
        sock.close.assert_called_once()

    def test_a_hostname_is_never_resolved(self):
        with patch(f"{MOD}.socket.socket") as factory:
            assert _host_source_ip("printer.local") is None
        factory.assert_not_called()

    def test_ipv6_destination_is_refused(self):
        with patch(f"{MOD}.socket.socket") as factory:
            assert _host_source_ip("fe80::1") is None
        factory.assert_not_called()

    def test_an_unreachable_route_is_not_an_error(self):
        sock = MagicMock()
        sock.connect.side_effect = OSError("Network is unreachable")
        with patch(f"{MOD}.socket.socket", return_value=sock):
            assert _host_source_ip("192.168.98.170") is None


class TestExistingPrinter:
    async def test_all_healthy(self):
        with _Env(state=_state(connected=True, developer_mode=True)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        s = _statuses(result)
        assert result.overall == "ok"
        assert s == {
            "port_mqtt": "pass",
            "port_ftps": "pass",
            "port_rtsps": "pass",
            "network_mode": "pass",
            "subnet": "pass",
            "mqtt_auth": "pass",
            "developer_mode": "pass",
            "external_storage": "pass",
            "printer_publishing": "pass",
        }

    async def test_a_connected_printer_is_never_probed(self):
        """⚠️ The regression guard for the support bundle disturbing the farm.

        ``diagnostic_snapshot._run_connection_for`` passes credentials for
        EXISTING printers, which used to take the pre-add branch below and open
        a second MQTT session to a printer that is already connected — on the
        path a user takes while already having a problem, possibly mid-print.
        Bambu firmware tolerates few concurrent sessions; the measured cost was
        the live client reconnecting and the printer's request topic being
        disabled for the rest of the session.

        Every other test in this class omits the credentials, which is why the
        branch order looked correct for so long.
        """
        with _Env(state=_state(connected=True)) as env:
            result = await run_connection_diagnostic(
                "192.168.1.50",
                printer=_printer(),
                serial_number="01P00A000000000",
                access_code="12345678",
            )

        assert _statuses(result)["mqtt_auth"] == "pass"
        env.test_connection.assert_not_called()

    async def test_a_printer_whose_client_is_down_is_still_probed(self):
        """There is no live session to disturb, and the probe is what separates
        "offline" from "wrong access code"."""
        with _Env(state=_state(connected=False)) as env:
            result = await run_connection_diagnostic(
                "192.168.1.50",
                printer=_printer(),
                serial_number="01P00A000000000",
                access_code="12345678",
            )

        assert _statuses(result)["mqtt_auth"] == "pass"
        env.test_connection.assert_called_once()

    async def test_mqtt_port_unreachable_is_a_problem(self):
        with _Env(ports=_port_probe({8883: False}), state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        s = _statuses(result)
        assert result.overall == "problems"
        assert s["port_mqtt"] == "fail"
        # Auth can't be judged when the broker port itself is closed.
        assert s["mqtt_auth"] == "skip"

    async def test_ftps_and_rtsps_only_warn(self):
        with _Env(ports=_port_probe({990: False, 322: False}), state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        s = _statuses(result)
        # No critical failure -> warnings, not problems.
        assert result.overall == "warnings"
        assert s["port_ftps"] == "warn"
        assert s["port_rtsps"] == "warn"

    async def test_an_open_port_990_that_speaks_no_tls_is_named(self):
        """The port answered, so "make sure port 990 is not blocked" is the wrong
        advice — the file service turned the connection away (upstream 91acac2b)."""
        with _Env(state=_state(), ftps_handshake="no_tls"):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        check = next(c for c in result.checks if c.id == "port_ftps")
        assert check.status == "warn"
        assert check.params == {"reason": "no_tls"}

    async def test_a_closed_port_990_keeps_the_plain_warning(self):
        with _Env(ports=_port_probe({990: False}), state=_state(), ftps_handshake="ok"):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        check = next(c for c in result.checks if c.id == "port_ftps")
        assert check.status == "warn"
        assert not check.params

    async def test_a1_mini_uses_chamber_image_camera_port(self):
        # A1/P1-family printers use the chamber-image camera protocol on 6000,
        # not RTSPS on 322. A closed 322 must not create a false camera warning.
        with _Env(ports=_port_probe({322: False, 6000: True}), state=_state()):
            result = await run_connection_diagnostic(
                "192.168.1.50",
                printer=_printer(model="A1 Mini"),
            )
        assert _statuses(result)["port_rtsps"] == "pass"
        camera_check = next(c for c in result.checks if c.id == "port_rtsps")
        assert camera_check.params == {"port": 6000, "protocol": "Chamber Image"}

    async def test_rtsp_models_still_probe_rtsps_port(self):
        with _Env(ports=_port_probe({322: False, 6000: True}), state=_state()):
            result = await run_connection_diagnostic(
                "192.168.1.50",
                printer=_printer(model="X1C"),
            )
        assert _statuses(result)["port_rtsps"] == "warn"
        camera_check = next(c for c in result.checks if c.id == "port_rtsps")
        assert camera_check.params == {"port": 322, "protocol": "RTSPS"}

    async def test_developer_mode_off_is_a_problem(self):
        with _Env(state=_state(connected=True, developer_mode=False)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        s = _statuses(result)
        assert s["developer_mode"] == "fail"
        assert result.overall == "problems"

    async def test_developer_mode_skipped_when_disconnected(self):
        # No live MQTT connection -> developer_mode can't be read.
        with _Env(state=_state(connected=False)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        s = _statuses(result)
        assert s["developer_mode"] == "skip"
        # Reachable port but no connection -> credential failure class.
        assert s["mqtt_auth"] == "fail"

    async def test_external_storage_passes_when_store_to_sdcard_true(self):
        # Install step 4 on: home_flag bit 11 -> state.store_to_sdcard=True.
        with _Env(state=_state(store_to_sdcard=True)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["external_storage"] == "pass"

    async def test_external_storage_fails_when_store_to_sdcard_false(self):
        with _Env(state=_state(store_to_sdcard=False)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        s = _statuses(result)
        assert s["external_storage"] == "fail"
        assert result.overall == "problems"

    async def test_external_storage_skipped_when_disconnected(self):
        # No live MQTT push -> the latest store_to_sdcard value can't be trusted.
        with _Env(state=_state(connected=False, store_to_sdcard=True)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["external_storage"] == "skip"

    async def test_external_storage_skipped_when_field_missing(self):
        # State exists + connected but store_to_sdcard was never populated
        # (older firmware that doesn't push the flag) -> skip, not a false fail.
        state = _state(store_to_sdcard=True)
        del state.store_to_sdcard
        with _Env(state=state):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["external_storage"] == "skip"

    async def test_skips_on_a1_no_external_storage_slot(self):
        # Regression for #1703: A1 and A1 Mini ship without a MicroSD slot
        # at all, so home_flag bit 11 is never set and a naive read would
        # report `fail` for every A1-series user. The model-aware skip
        # branch suppresses that — and the overall result must NOT escalate
        # to "problems" purely because of this check.
        with _Env(state=_state(store_to_sdcard=False)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer(model="A1"))
        assert _statuses(result)["external_storage"] == "skip"
        assert result.overall == "ok"

    async def test_skips_on_a1_mini_no_external_storage_slot(self):
        with _Env(state=_state(store_to_sdcard=False)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer(model="A1 Mini"))
        assert _statuses(result)["external_storage"] == "skip"

    async def test_skips_on_p1s_with_no_reachable_toggle(self):
        # #2524: the P1-series has an SD slot but no way to turn the option on —
        # BambuStudio only renders the toggle for models declaring
        # support_save_remote_print_file_to_storage (P1 never does) and the
        # printer has no screen. A fail there is permanently unresolvable, so
        # the check reports skip + a reason the UI explains, and the overall
        # result must not escalate.
        with _Env(state=_state(store_to_sdcard=False)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer(model="P1S"))
        check = next(c for c in result.checks if c.id == "external_storage")
        assert check.status == "skip"
        assert check.params == {"reason": "unsupported_model"}
        assert result.overall == "ok"

    async def test_skips_on_a2l_with_no_reachable_toggle(self):
        # BamDude divergence from upstream's hardcoded {P1S, P1P} set: reading
        # the mirrored BS configs also catches A2L, which has an SD slot and
        # likewise never declares the capability.
        with _Env(state=_state(store_to_sdcard=False)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer(model="A2L"))
        check = next(c for c in result.checks if c.id == "external_storage")
        assert check.status == "skip"
        assert check.params == {"reason": "unsupported_model"}

    async def test_p1s_reporting_the_option_on_still_passes(self):
        # The skip only covers the "off with no way to turn it on" case.
        with _Env(state=_state(store_to_sdcard=True)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer(model="P1S"))
        assert _statuses(result)["external_storage"] == "pass"

    async def test_live_capability_reactivates_the_fail(self):
        # Self-healing half of the hybrid: a firmware that starts reporting
        # support_save_remote_print_file_to_storage makes the toggle reachable
        # again, so the fail becomes actionable without a code change.
        state = _state(store_to_sdcard=False)
        state.print_option_support = {"save_remote_to_storage": True}
        with _Env(state=state):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer(model="P1S"))
        assert _statuses(result)["external_storage"] == "fail"

    async def test_still_fails_on_x1c_when_toggle_off(self):
        # Sanity: the model-aware skip MUST NOT silently let X1C-class
        # printers off the hook. The store_to_sdcard=False path is the
        # one real bit of value this check provides for those models.
        with _Env(state=_state(store_to_sdcard=False)):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer(model="X1C"))
        assert _statuses(result)["external_storage"] == "fail"

    async def test_printer_publishing_passes_when_reports_seen(self):
        # Counter > 0 means the printer is publishing on the report topic.
        with _Env(state=_state(), report_messages_since_connect=1):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["printer_publishing"] == "pass"

    async def test_printer_publishing_fails_when_zero_reports_after_wait(self):
        # Counter stays at 0 across the wait window — printer never published.
        with _Env(state=_state(), report_messages_since_connect=0):
            result = await run_connection_diagnostic(
                "192.168.1.50",
                printer=_printer(),
                wait_for_publish_seconds=0.05,
            )
        s = _statuses(result)
        assert s["printer_publishing"] == "fail"
        assert result.overall == "problems"
        # The check exposes the wait budget so the UI can render a countdown.
        params = next(c.params for c in result.checks if c.id == "printer_publishing")
        assert params == {"max_wait_seconds": 0.05}

    async def test_printer_publishing_skips_when_disconnected(self):
        with _Env(state=_state(connected=False), report_messages_since_connect=0):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["printer_publishing"] == "skip"

    async def test_printer_publishing_skips_when_no_client(self):
        # State says connected but printer_manager has no client object
        # (race between client teardown and a fresh diagnostic request).
        with _Env(state=_state(), report_messages_since_connect=None):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["printer_publishing"] == "skip"

    async def test_printer_publishing_no_wait_returns_instantly_on_zero(self):
        # Default wait is 0 — instant pass/fail without polling (support-package
        # code path, keeps bundling fast).
        with _Env(state=_state(), report_messages_since_connect=0):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        s = _statuses(result)
        assert s["printer_publishing"] == "fail"
        params = next(c.params for c in result.checks if c.id == "printer_publishing")
        assert params == {}

    async def test_bridge_mode_warns_and_skips_subnet(self):
        with _Env(network_mode="bridge", state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        s = _statuses(result)
        assert s["network_mode"] == "warn"
        # Container IP isn't the host IP in bridge mode -> subnet check is meaningless.
        assert s["subnet"] == "skip"

    async def test_network_mode_skipped_outside_a_container(self):
        with _Env(runtime=None, state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["network_mode"] == "skip"

    async def test_podman_host_networking_passes(self):
        """upstream #3092: the same two shapes as Docker, read the same way."""
        with _Env(runtime="Podman", network_mode="host", state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        check = _check(result, "network_mode")
        assert check.status == "pass"
        assert check.params["runtime"] == "Podman"

    async def test_podman_bridge_networking_warns(self):
        with _Env(runtime="Podman", network_mode="bridge", state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["network_mode"] == "warn"

    async def test_undetectable_mode_skips_rather_than_guessing(self):
        """A container we cannot read must not be told to recreate itself."""
        with _Env(runtime="Podman", network_mode=None, state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        check = _check(result, "network_mode")
        assert check.status == "skip"
        assert check.params == {"reason": "unknown", "runtime": "Podman"}

    async def test_system_container_has_no_network_mode_to_recommend(self):
        """LXC/LXD is bridged onto the LAN like a small VM — nothing to fix."""
        with _Env(runtime="LXC", state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        check = _check(result, "network_mode")
        assert check.status == "skip"
        assert check.params == {"reason": "system_container", "runtime": "LXC"}
        assert _statuses(result)["subnet"] == "pass"

    async def test_different_subnet_warns(self):
        with _Env(host_ip="10.0.0.5", host_subnet="10.0.0.0/24", state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["subnet"] == "warn"

    async def test_a_wider_lan_is_not_a_different_subnet(self):
        """upstream #3092 end to end, with the reporter's own addresses."""
        with _Env(host_ip="192.168.96.9", host_subnet="192.168.96.0/22", state=_state()):
            result = await run_connection_diagnostic("192.168.98.170", printer=_printer(ip="192.168.98.170"))
        assert _statuses(result)["subnet"] == "pass"

    async def test_unknown_host_prefix_skips_rather_than_warning(self):
        with _Env(host_ip="192.168.96.9", host_subnet=None, state=_state()):
            result = await run_connection_diagnostic("192.168.98.170", printer=_printer(ip="192.168.98.170"))
        assert _statuses(result)["subnet"] == "skip"


class TestPreAddFlow:
    async def test_bad_credentials_fail_mqtt_auth(self):
        with _Env(test_connection_success=False):
            result = await run_connection_diagnostic("192.168.1.50", serial_number="01P", access_code="wrong")
        s = _statuses(result)
        assert s["mqtt_auth"] == "fail"
        # No saved printer -> developer mode can't be read.
        assert s["developer_mode"] == "skip"

    async def test_good_credentials_pass_mqtt_auth(self):
        with _Env(test_connection_success=True):
            result = await run_connection_diagnostic("192.168.1.50", serial_number="01P", access_code="right")
        assert _statuses(result)["mqtt_auth"] == "pass"

    async def test_no_credentials_skips_mqtt_auth(self):
        with _Env():
            result = await run_connection_diagnostic("192.168.1.50")
        assert _statuses(result)["mqtt_auth"] == "skip"


def _signature_probe(signed):
    """Patch ``_interpreter_is_signed`` to answer ``signed``."""
    probe = MagicMock(return_value=signed)
    return patch(f"{MOD}._interpreter_is_signed", probe), probe


class TestMacosLocalNetworkCheck:
    """The macOS Local Network (TCC) check (upstream 1ccaf74d, #3114).

    macOS attributes the permission to a code signature. An unsigned
    interpreter has no identity to anchor a grant to, so every connection to
    the printer is dropped with no error and no prompt — the ports read as
    unreachable and nothing says why.
    """

    async def test_absent_on_other_platforms(self):
        with _Env(platform="linux", state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert "macos_local_network" not in _statuses(result)

    async def test_passes_when_the_control_port_answers(self):
        patcher, probe = _signature_probe(False)
        with patcher, _Env(platform="darwin", state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["macos_local_network"] == "pass"
        probe.assert_not_called()

    async def test_unsigned_interpreter_names_the_repair(self):
        patcher, _probe = _signature_probe(False)
        with patcher, _Env(platform="darwin", ports=_port_probe({8883: False}), state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        check = _check(result, "macos_local_network")
        assert check.status == "warn"
        assert check.params["reason"] == "unsigned"
        assert check.params["executable"]

    async def test_signed_interpreter_points_at_system_settings(self):
        patcher, _probe = _signature_probe(True)
        with patcher, _Env(platform="darwin", ports=_port_probe({8883: False}), state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        check = _check(result, "macos_local_network")
        assert check.status == "warn"
        assert check.params["reason"] == "permission"

    async def test_undeterminable_signature_is_not_reported_as_unsigned(self):
        patcher, _probe = _signature_probe(None)
        with patcher, _Env(platform="darwin", ports=_port_probe({8883: False}), state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _check(result, "macos_local_network").params["reason"] == "permission"

    async def test_never_turns_a_healthy_result_red(self):
        patcher, _probe = _signature_probe(False)
        with patcher, _Env(platform="darwin", state=_state(), report_messages_since_connect=42):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert result.overall == "ok"


class TestInterpreterSignatureProbe:
    """``codesign`` has three outcomes and they must stay distinguishable."""

    def _run(self, **kwargs):
        return patch(f"{MOD}.subprocess.run", **kwargs)

    def test_zero_exit_means_signed(self):
        with self._run(return_value=types.SimpleNamespace(returncode=0, stderr="")):
            assert _interpreter_is_signed() is True

    def test_not_signed_at_all_means_unsigned(self):
        stderr = "/usr/local/.../python3.14: code object is not signed at all"
        with self._run(return_value=types.SimpleNamespace(returncode=1, stderr=stderr)):
            assert _interpreter_is_signed() is False

    def test_other_failure_is_undeterminable(self):
        with self._run(return_value=types.SimpleNamespace(returncode=1, stderr="No such file or directory")):
            assert _interpreter_is_signed() is None

    def test_probe_failure_never_raises(self):
        with self._run(side_effect=OSError("boom")):
            assert _interpreter_is_signed() is None
        with self._run(side_effect=subprocess.TimeoutExpired(cmd="codesign", timeout=5.0)):
            assert _interpreter_is_signed() is None
