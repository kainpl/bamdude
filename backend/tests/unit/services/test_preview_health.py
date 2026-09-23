"""Diagnostics must survive fail-soft cleanup, without becoming a broker probe."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.services import preview_runtime as module
from backend.app.services.preview_runtime import PreviewRuntime


def test_health_without_runtime(monkeypatch):
    monkeypatch.setattr(module, "runtime", None)
    assert module.get_preview_health().model_dump() == {
        "state": "unavailable",
        "reason": "not_started",
        "error_type": None,
        "runtime_dir": None,
        "recovery_required": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", [False, True])
async def test_startup_reason_survives_cleanup(tmp_path, monkeypatch, caplog, recovery):
    from embedded_nats import RecoveryRequired

    service = PreviewRuntime(tmp_path / "preview")
    error = (RecoveryRequired if recovery else OSError)("secret-token-must-not-be-exposed")
    monkeypatch.setattr(service, "_directories", Mock(side_effect=error))
    await service.start()
    health = service.health()
    assert service.closed and not service.ready
    assert health.state == "unavailable"
    assert health.reason == ("recovery_required" if recovery else "startup_failed")
    assert health.recovery_required is recovery
    assert health.error_type == type(error).__name__
    assert str(service.root) in caplog.text
    assert module.RECOVERY_GUIDE in caplog.text
    assert "secret-token" not in caplog.text
    assert "secret-token" not in health.model_dump_json()
    await service.stop()
    assert service.health() == health


@pytest.mark.asyncio
async def test_missing_dependency_remains_fail_soft(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "embedded_nats", None)
    service = PreviewRuntime(tmp_path / "preview")
    await service.start()
    assert service.health().reason == "startup_failed"


@pytest.mark.asyncio
async def test_disconnect_and_uncertain_owner_are_visible(tmp_path):
    service = PreviewRuntime(tmp_path)
    service.ready = True
    await service.disconnected()
    assert service.health().reason == "broker_disconnected"
    service.service = SimpleNamespace(stop=Mock(side_effect=OSError("cannot reap")))
    with pytest.raises(OSError):
        await service._retire()
    health = service.health()
    assert health.reason == "ownership_uncertain"
    assert health.recovery_required
    assert not service.ready


@pytest.mark.asyncio
async def test_launch_clears_old_failure_and_stop_does_not_invent_disconnect(tmp_path, monkeypatch):
    service = PreviewRuntime(tmp_path)
    service._unavailable("broker_disconnected", OSError("old failure"))
    service.staging = tmp_path / "staging"
    service.broker = SimpleNamespace(url="nats://127.0.0.1:1234", stop=Mock())
    service.token = "never-return-this"
    child = SimpleNamespace(process=SimpleNamespace(poll=lambda: None), stop=Mock())
    monkeypatch.setattr(module, "PreviewProcess", Mock(return_value=child))

    async def ready(command):
        return {
            "outcome": "ok",
            "epoch": command.service_epoch,
            "protocol_version": 1,
            "monotonic_ns": time.monotonic_ns(),
        }

    monkeypatch.setattr(service, "_rpc", ready)
    await service._launch()
    assert service.health().state == "ready"
    assert service.health().reason is None
    assert service.health().error_type is None
    assert service.token not in service.health().model_dump_json()
    service.nc = SimpleNamespace(close=service.disconnected)
    await service.stop()
    assert service.health().reason == "stopped"


@pytest.mark.asyncio
async def test_monitor_exposes_backoff_and_circuit_without_health_probes(tmp_path, monkeypatch):
    service = PreviewRuntime(tmp_path)
    service.nc = SimpleNamespace(is_connected=True)
    service.restarts = [time.monotonic()] * 3
    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            service.closed = True

    monkeypatch.setattr(asyncio, "sleep", tick)
    service._launch = AsyncMock()
    await service._monitor()
    service.closed = False
    service.nc = Mock()  # A snapshot must not interrogate the network/process.
    service.service = Mock()
    assert service.health().state == "recovering"
    assert service.health().reason == "circuit_open"
    service.circuit_until = 0
    assert service.health().reason == "worker_restarting"
    service.nc.assert_not_called()
    assert not service.nc.mock_calls
    assert not service.service.mock_calls
    service._launch.assert_not_awaited()


def test_snapshot_contains_only_diagnostic_fields(tmp_path, monkeypatch):
    service = PreviewRuntime(tmp_path)
    service.token = "secret"
    monkeypatch.setattr(module, "runtime", service)
    result = json.loads(module.get_preview_health().model_dump_json())
    assert set(result) == {"state", "reason", "error_type", "runtime_dir", "recovery_required"}


def test_uncertain_cleanup_overrides_previously_ready_state(tmp_path):
    service = PreviewRuntime(tmp_path)
    service.ready = True
    service.uncertain = True
    assert service.health().state == "unavailable"
    assert service.health().reason == "ownership_uncertain"
    assert service.health().recovery_required
