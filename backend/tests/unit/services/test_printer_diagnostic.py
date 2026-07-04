"""Unit tests for the connection diagnostic.

Pins the pass / fail / warn / skip contract of each check. Those statuses
drive the localized fix text the user sees when a printer won't connect,
so a status flip is a user-facing regression — each one is asserted here.
"""

import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.services.printer_diagnostic import _same_subnet, run_connection_diagnostic

MOD = "backend.app.services.printer_diagnostic"


def _statuses(result):
    """Map of check id -> status for concise assertions."""
    return {c.id: c.status for c in result.checks}


def _port_probe(overrides=None):
    """Sync side_effect for _check_port. Defaults: every port reachable."""
    reachable = {8883: True, 990: True, 322: True}
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
        in_docker=True,
        network_mode="host",
        host_ip="192.168.1.5",
        state=None,
        test_connection_success=True,
        report_messages_since_connect: int | None = 5,
    ):
        self.ports = ports or _port_probe()
        self.in_docker = in_docker
        self.network_mode = network_mode
        self.host_ip = host_ip
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
        if self.report_messages_since_connect is None:
            manager.get_client.return_value = None
        else:
            client = MagicMock()
            client.report_messages_since_connect = self.report_messages_since_connect
            manager.get_client.return_value = client
        self._stack.enter_context(patch(f"{MOD}._check_port", new_callable=AsyncMock, side_effect=self.ports))
        self._stack.enter_context(patch(f"{MOD}.is_running_in_docker", return_value=self.in_docker))
        self._stack.enter_context(patch(f"{MOD}._detect_docker_network_mode", return_value=self.network_mode))
        self._stack.enter_context(patch(f"{MOD}._get_host_ip", return_value=self.host_ip))
        self._stack.enter_context(patch(f"{MOD}.printer_manager", manager))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False


def _printer(ip="192.168.1.50"):
    return types.SimpleNamespace(id=1, ip_address=ip)


class TestSameSubnet:
    def test_same_24(self):
        assert _same_subnet("192.168.1.10", "192.168.1.200") is True

    def test_different_24(self):
        assert _same_subnet("192.168.1.10", "192.168.2.10") is False

    def test_hostname_undeterminable(self):
        assert _same_subnet("printer.local", "192.168.1.10") is None

    def test_ipv6_undeterminable(self):
        assert _same_subnet("fe80::1", "192.168.1.10") is None


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

    async def test_network_mode_skipped_outside_docker(self):
        with _Env(in_docker=False, state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["network_mode"] == "skip"

    async def test_different_subnet_warns(self):
        with _Env(host_ip="10.0.0.5", state=_state()):
            result = await run_connection_diagnostic("192.168.1.50", printer=_printer())
        assert _statuses(result)["subnet"] == "warn"


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
