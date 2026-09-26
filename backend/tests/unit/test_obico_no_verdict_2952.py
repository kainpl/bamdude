"""A printer nothing is checking is not reported Safe (upstream 06e5114a, #2952).

The state entry is created when a monitored print is first seen, before the
first frame, and ``get_per_printer`` answered ``safe`` for any printer without a
verdict — so a rejected ML token, an unreachable ML API, a failed capture or an
unset External URL rendered as a healthy watched print. For a safety feature
that is the worst failure there is: it says the print is watched exactly when
it is not. Two honest classes: ``error`` (the last poll produced no verdict,
with the reason, per printer) and ``unknown`` (watched, no result yet).
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from backend.app.api.routes import obico as obico_routes
from backend.app.services import obico_detection as od
from backend.app.services.obico_detection import ObicoDetectionService, PrintState


def _service_watching(*printer_ids):
    service = ObicoDetectionService()
    for pid in printer_ids:
        service._states[pid] = PrintState()
        service._state_keys[pid] = "job"
    return service


def test_a_watched_print_without_a_result_is_starting_not_safe():
    service = _service_watching(1)
    entry = service.get_per_printer()[1]
    assert entry["class"] == "unknown"
    assert entry["error"] is None


def test_a_poll_that_produced_no_verdict_is_not_checking_with_its_own_reason():
    service = _service_watching(1, 2)
    service._last_class[2] = "safe"
    service._no_verdict(1, "Failed to capture snapshot for printer 1")
    per = service.get_per_printer()
    assert per[1]["class"] == "error" and per[1]["error"] == "Failed to capture snapshot for printer 1"
    assert per[2]["class"] == "safe" and per[2]["error"] is None


def test_an_error_masks_an_older_verdict():
    # A print that WAS safe and has since lost its camera is not watched now.
    service = _service_watching(1)
    service._last_class[1] = "safe"
    service._no_verdict(1, "ML API call failed for printer 1: timeout")
    assert service.get_per_printer()[1]["class"] == "error"


@pytest.mark.asyncio
async def test_each_no_verdict_exit_records_the_printer(monkeypatch):
    service = _service_watching(1)

    async def no_frame(pid):
        return None

    monkeypatch.setattr(service, "_capture_frame", no_frame)
    await service._check_printer(
        1, SimpleNamespace(task_name="job"), {"external_url": "http://x", "ml_url": "http://m"}
    )
    assert service.get_per_printer()[1]["class"] == "error"

    async def a_frame(pid):
        return b"jpeg"

    monkeypatch.setattr(service, "_capture_frame", a_frame)
    await service._check_printer(1, SimpleNamespace(task_name="job"), {"external_url": "", "ml_url": "http://m"})
    assert "external_url" in service.get_per_printer()[1]["error"]


@pytest.mark.asyncio
async def test_a_successful_check_clears_the_reason(monkeypatch):
    service = _service_watching(1)
    service._no_verdict(1, "earlier failure")

    async def a_frame(pid):
        return b"jpeg"

    async def stash(frame):
        return "nonce"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            return httpx.Response(200, json={"detections": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(service, "_capture_frame", a_frame)
    monkeypatch.setattr(od, "stash_frame", stash)
    monkeypatch.setattr(od.httpx, "AsyncClient", _Client)
    await service._check_printer(
        1,
        SimpleNamespace(task_name="job"),
        {"external_url": "http://x", "ml_url": "http://m", "sensitivity": "medium", "action": "notify"},
    )
    entry = service.get_per_printer()[1]
    assert entry["class"] == "safe" and entry["error"] is None


@pytest.mark.asyncio
async def test_the_reason_stays_behind_settings_read_but_the_class_does_not(monkeypatch):
    service = _service_watching(1)
    service._no_verdict(1, "connect to http://ml.internal:3333 failed")
    monkeypatch.setattr(obico_routes, "obico_detection_service", service)

    async def fake_settings():
        return {"enabled": True, "enabled_printers": None}

    monkeypatch.setattr(service, "_load_settings", fake_settings)
    operator = SimpleNamespace(has_permission=lambda p: p == "printers:read")
    body = await obico_routes.get_printer_status(user=operator)
    assert body["per_printer"][1]["class"] == "error"
    assert body["per_printer"][1]["error"] is None

    admin = SimpleNamespace(has_permission=lambda p: p in ("printers:read", "settings:read"))
    body = await obico_routes.get_printer_status(user=admin)
    assert "ml.internal" in body["per_printer"][1]["error"]
