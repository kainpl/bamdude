"""Keep actual producer display contracts without broadening their audience."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.core.permissions import Permission
from backend.app.services import background_dispatch, firmware_batch, live_projection
from backend.app.services.live_authority import Entitlements


def audience(*permissions, printers=None):
    return Entitlements(frozenset(permissions), None if printers is None else frozenset(printers), 7, "account")


def administrator():
    return audience(*(permission.value for permission in Permission))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [("unavailable", "plate_check_unavailable"), ("occupied", "plate_objects")])
async def test_real_plate_alert_preserves_printer_and_reason_for_permitted_viewer(monkeypatch, status, reason):
    from backend.app import main
    from backend.app.services import plate_detection

    printer = SimpleNamespace(
        id=1,
        name="Workshop P1",
        ip_address="unused",
        access_code="unused",
        model="P1S",
        external_camera_url=None,
        external_camera_type=None,
        external_camera_enabled=False,
        external_camera_snapshot_url=None,
        plate_detection_roi=None,
        plate_detection_polygon=[{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}],
    )
    detector = plate_detection.PlateDetectionResult(False, 50, 50, "detector result", status=status)
    monkeypatch.setattr(plate_detection, "check_plate_empty", AsyncMock(return_value=detector))
    monkeypatch.setattr(main, "printer_manager", Mock())
    monkeypatch.setattr(main, "set_expected_pause_reason", Mock())
    broadcast = AsyncMock()
    monkeypatch.setattr(main.ws_manager, "broadcast", broadcast)
    monkeypatch.setattr(main.notification_service, "on_plate_not_empty", AsyncMock())
    await main._check_plate_at_print_start(printer, None)
    payload = broadcast.await_args.args[0]
    assert payload["reason"] == reason
    assert ("unavailable" in payload["message"]) == (status == "unavailable")
    prepared = await live_projection.prepare(payload, source_printer_identities={1: "printer-1"})
    # Layout selects its text by reason; dropping it mislabels unavailable as occupied.
    assert live_projection.shape(prepared, administrator()) == payload
    assert live_projection.shape(prepared, audience("printers:read")) == payload
    assert live_projection.shape(prepared, audience("websocket:connect")) is None
    assert live_projection.shape(prepared, audience("printers:read", printers=[2])) is None


@pytest.mark.asyncio
async def test_real_firmware_batch_status_text_requires_firmware_permission_and_visible_printer(monkeypatch):
    broadcast = AsyncMock()
    monkeypatch.setattr(firmware_batch.ws_manager, "broadcast", broadcast)
    await firmware_batch._broadcast(5, 1, "uploading", "Uploading firmware", 40)
    payload = broadcast.await_args.args[0]
    prepared = await live_projection.prepare(payload, source_printer_identities={1: "printer-1"})
    # FirmwareUpdatePage renders message alongside the percent/status.
    assert live_projection.shape(prepared, administrator()) == payload
    assert live_projection.shape(prepared, audience("firmware:read")) == payload
    assert live_projection.shape(prepared, audience("printers:read")) is None
    assert live_projection.shape(prepared, audience("firmware:read", printers=[2])) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,family,domain",
    [
        ("print_library_file", "library", "library"),
        ("reprint_archive", "archive", "archives"),
    ],
)
@pytest.mark.parametrize("source_id", [None, 3])
@pytest.mark.parametrize(
    "reader,owner,credential_kind,visible",
    [
        ("all", 8, "session", True),
        ("own", 7, "session", True),
        ("own", 8, "session", False),
        ("own", 7, "key", False),
        ("none", 7, "session", False),
    ],
)
async def test_real_dispatch_progress_and_failure_keep_only_authorized_source_display(
    monkeypatch, kind, family, domain, source_id, reader, owner, credential_kind, visible
):
    service = background_dispatch.BackgroundDispatchService()
    job = background_dispatch.PrintDispatchJob(
        id=9, kind=kind, source_id=source_id, source_name="captured-benchy.3mf", printer_id=1, printer_name="P1"
    )
    service._active_jobs[job.id] = background_dispatch.ActiveDispatchState(job=job, message="Preparing")
    service._batch_total = 1
    broadcast = AsyncMock()
    monkeypatch.setattr(background_dispatch.ws_manager, "broadcast", broadcast)
    metadata_calls = []

    async def rows(refs):
        metadata_calls.append(refs)
        return {(family, 3): {"id": 3, "created_by_id": owner, "printer_id": 1, "filename": "current-benchy.3mf"}}

    monkeypatch.setattr(live_projection.live_authority, "resource_rows", rows)
    await service._set_active_message(job, "Uploading to P1...", phase="uploading")
    failure = "This printer needs an SD card to accept a print, and none is inserted."
    await service._mark_job_finished(job, failed=True, message=failure)
    permissions = ["queue:read_own"] + ([f"{domain}:read_{reader}"] if reader != "none" else [])
    access = audience(*permissions)
    display_allowed = visible and (source_id is not None or reader == "all")
    for index, call in enumerate(broadcast.await_args_list):
        payload = call.args[0]
        recent = payload["data"]["recent_event"]
        assert recent["kind"] == kind and recent["source_id"] == source_id
        prepared = await live_projection.prepare(payload, source_printer_identities={1: "printer-1"})
        out = live_projection.shape(prepared, access, credential_kind=credential_kind)["data"]
        projected = [out["recent_event"]]
        if index == 0:
            assert out["active_jobs"] and out["active_job"] == out["active_jobs"][0]
            projected += out["active_jobs"]
        else:
            assert not out["active_jobs"] and out["recent_event"]["status"] == "failed"
        for display in projected:
            assert display["job_id"] == job.id and display["printer_name"] == "P1"
            if display_allowed:
                assert display["message"] == ("Uploading to P1..." if index == 0 else failure)
                assert display["source_name"] == ("current-benchy.3mf" if source_id else job.source_name)
            else:
                assert "source_name" not in display and "message" not in display and "source_id" not in display
        assert live_projection.shape(prepared, audience("printers:read")) is None
        scoped = live_projection.shape(prepared, audience(*permissions, printers=[2]))["data"]
        assert not scoped["active_jobs"] and scoped["recent_event"] is None
        # Full-rights consumers see the same fields the toast actually renders.
        full = live_projection.shape(prepared, administrator())["data"]["recent_event"]
        assert full["message"] == recent["message"] and full["source_name"]
    assert metadata_calls == ([{(family, 3)}] * 2 if source_id else [])


def test_every_dispatch_recent_event_carries_its_exact_job_source_for_projection():
    tree = ast.parse(Path("backend/app/services/background_dispatch.py").read_text(encoding="utf-8"))
    producers = [node for node in ast.walk(tree) if isinstance(node, ast.keyword) and node.arg == "recent_event"]
    assert producers
    for producer in producers:
        fields = {key.value: value for key, value in zip(producer.value.keys, producer.value.values, strict=True)}
        job = fields["job_id"].value
        assert ast.dump(fields["kind"]) == ast.dump(ast.Attribute(value=job, attr="kind", ctx=ast.Load()))
        assert ast.dump(fields["source_id"]) == ast.dump(ast.Attribute(value=job, attr="source_id", ctx=ast.Load()))


@pytest.mark.parametrize("kind,source_id", [("unknown", None), ("print_library_file", 3)])
def test_full_reader_cannot_borrow_captured_source_display_for_unknown_or_missing_addressed_source(kind, source_id):
    raw = {
        "job_id": 2,
        "kind": kind,
        "source_id": source_id,
        "printer_id": 1,
        "source_name": "private",
        "message": "private source failure",
    }
    prepared = live_projection.PreparedEvent({"type": "background_dispatch", "data": {"recent_event": raw}})
    out = live_projection.shape(prepared, administrator())["data"]["recent_event"]
    assert "source_name" not in out and "message" not in out and "source_id" not in out
