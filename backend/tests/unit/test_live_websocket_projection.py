"""Delivery boundaries and policy classes of actual WS event paths."""

import ast
import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from backend.app.core.auth_lifetime import FrozenAuthority
from backend.app.core.websocket import ConnectionManager
from backend.app.services import live_projection
from backend.app.services.live_authority import Entitlements, LiveDescriptor, LiveHandle, live_authority


def access(*permissions, printers=None, user_id=7):
    return Entitlements(frozenset(permissions), None if printers is None else frozenset(printers), user_id, "account")


def event(kind, **fields):
    return live_projection.PreparedEvent({"type": kind, **fields})


def test_websocket_permission_alone_does_not_open_resources_or_unknown_events():
    permissions = access("websocket:connect")
    for kind in live_projection.KNOWN_EVENTS - live_projection.CONTROL_EVENTS:
        assert live_projection.shape(event(kind, printer_id=1, data={"filename": "private"}), permissions) is None
    assert live_projection.shape(event("future_private_event", data={"secret": "private"}), permissions) is None
    assert live_projection.shape(event("pong"), permissions) == {"type": "pong"}


def test_every_actual_status_field_is_explicitly_projected_or_privacy_masked():
    source = Path("backend/app/services/printer_manager.py").read_text(encoding="utf-8")
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "printer_state_to_dict"
    )
    result = next(
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "result" for target in node.targets)
        and isinstance(node.value, ast.Dict)
    )
    producer_fields = {key.value for key in result.keys if isinstance(key, ast.Constant)}
    # cover_url is assigned after constructing the dictionary.
    producer_fields.add("cover_url")
    private = {"current_print", "subtask_name", "gcode_file", "cover_url"}
    assert producer_fields == live_projection.STATUS_FIELDS | private
    prepared = event("printer_status", printer_id=1, data={"mqtt_recording": True, "mqtt_recording_bytes": 12345})
    assert live_projection.shape(prepared, access("printers:read"))["data"]["mqtt_recording_bytes"] == 12345


def test_registry_covers_actual_literal_and_dynamic_producer_doors():
    root = Path(__file__).resolve().parents[2] / "app"
    found = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = ast.unparse(node.func.value)
            direct = owner == "ws_manager" and node.func.attr in {"send", "broadcast", "broadcast_to_user"}
            wrapper = path.name == "websocket.py" and owner == "self" and node.func.attr == "broadcast"
            zigbee = path.as_posix().endswith("zigbee/coordinator.py") and node.func.attr == "_emit"
            if not (direct or wrapper or zigbee):
                continue
            for arg in node.args:
                if not isinstance(arg, ast.Dict):
                    continue
                for key, value in zip(arg.keys, arg.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "type":
                        if isinstance(value, ast.Constant):
                            found.add(value.value)
                        elif isinstance(value, ast.Name) and value.id == "EVENT":
                            assert path.name == "part_images.py"
                            found.add("part_images_changed")
                        else:
                            assert (
                                path.name == "calibration_service.py" and ast.unparse(value) == "f'calibration.{event}'"
                            )
    assert found <= live_projection.KNOWN_EVENTS
    assert {"printer_status", "inbox_item", "zigbee_device_paired", "part_images_changed"} <= found


@pytest.mark.parametrize("printers, expected", [(None, True), ([], False), ([1], True), ([2], False)])
def test_all_printer_event_paths_apply_key_ceiling(printers, expected):
    permissions = access("printers:read", "queue:read_all", printers=printers)
    for kind in (
        "printer_status",
        "print_start",
        "print_complete",
        "print_paused",
        "print_resumed",
        "queue_changed",
        "calibration.started",
    ):
        assert (
            live_projection.shape(event(kind, printer_id=1, data={"progress": 2}), permissions) is not None
        ) is expected


def test_status_preserves_operations_but_names_need_proven_current_owned_archive():
    prepared = event(
        "printer_status",
        printer_id=1,
        data={"progress": 25, "current_print": "private", "gcode_file": "secret.3mf", "cover_url": "private-url"},
    )
    prepared.run_archive, prepared.run_identity = 42, "old-row"
    prepared.rows = {
        ("archive", 42): {
            "id": 42,
            "created_by_id": 7,
            "printer_id": 1,
            "recovery_identity": "old-row",
            "filename": "private",
        }
    }
    own = access("printers:read", "archives:read_own")
    shaped = live_projection.shape(prepared, own)
    assert shaped["data"]["progress"] == 25 and shaped["data"]["current_print"] == "private"
    prepared.rows[("archive", 42)]["created_by_id"] = None
    assert live_projection.shape(prepared, own)["data"]["current_print"] is None
    prepared.rows[("archive", 42)]["created_by_id"] = 7
    prepared.rows[("archive", 42)]["recovery_identity"] = "new-row"
    assert live_projection.shape(prepared, own)["data"]["gcode_file"] is None
    assert prepared.message["data"]["gcode_file"] == "secret.3mf"


def test_workshop_keeps_restricted_print_invalidation_without_private_details():
    prepared = event("archive_updated", data={"id": 42, "filename": "other-owner.3mf", "failure_reason": "private"})
    shaped = live_projection.shape(prepared, access("orders:read"))
    assert shaped == {"type": "archive_updated", "data": {"restricted": True}}
    assert live_projection.shape(prepared, access("customers:read")) is None


@pytest.mark.parametrize(
    "owner,permission,visible",
    [
        (7, "library:read_own", True),
        (8, "library:read_own", False),
        (None, "library:read_own", False),
        (None, "library:read_all", True),
    ],
)
def test_library_invalidation_asks_current_ownership_not_old_payload(owner, permission, visible):
    prepared = event(
        "library_file_notes_changed", data={"file_id": 3, "notes_count": 55, "filename": "old-private-name"}
    )
    prepared.rows[("library", 3)] = {"id": 3, "created_by_id": owner, "filename": "new-private-name"}
    shaped = live_projection.shape(prepared, access(permission))
    assert (shaped is not None) is visible
    if shaped:
        assert shaped["data"] == {"file_id": 3}


def test_key_ownership_door_needs_all_permission_even_for_owners_own_row():
    prepared = event("library_file_notes_changed", data={"file_id": 3})
    prepared.rows[("library", 3)] = {"id": 3, "created_by_id": 7}
    assert live_projection.shape(prepared, access("library:read_own")) is not None
    assert live_projection.shape(prepared, access("library:read_own"), credential_kind="key") is None
    assert live_projection.shape(prepared, access("library:read_all"), credential_kind="key") is not None


def test_nested_macro_printer_cannot_bypass_printer_scope():
    prepared = event(
        "macro_executed", data={"printer_id": 2, "macro_name": "private", "status": "completed", "success": True}
    )
    assert live_projection.shape(prepared, access("printers:read", "settings:read", printers=[1])) is None
    assert live_projection.shape(prepared, access("printers:read", printers=[2])) == {
        "type": "macro_executed",
        "data": {"printer_id": 2, "status": "completed", "success": True},
    }


def test_scan_keeps_real_progress_fields_and_never_private_mount_error():
    prepared = event(
        "library_scan_finished",
        data={
            "job_id": 5,
            "folder_id": 2,
            "files_seen": 11,
            "files_added": 4,
            "warnings": 1,
            "status": "failed",
            "error": "private mount credentials",
            "path": "private mount",
        },
    )
    out = live_projection.shape(prepared, access("library:read_all"))["data"]
    assert out == {"job_id": 5, "folder_id": 2, "files_seen": 11, "files_added": 4, "warnings": 1, "status": "failed"}


@pytest.mark.parametrize("permission", ["products:read", "orders:read", "stock:read"])
def test_part_image_audience_is_any_workshop_label_reader(permission):
    prepared = event("part_images_changed", data={"product_ids": [4], "images_only": True})
    assert live_projection.shape(prepared, access(permission)) == prepared.message


def test_dispatch_filters_every_branch_and_recomputes_permitted_counts():
    prepared = event(
        "background_dispatch",
        data={
            "processing": 2,
            "dispatched": 1,
            "total": 55,
            "active_jobs": [
                {"job_id": 1, "printer_id": 1, "source_name": "hidden", "message": "secret"},
                {"job_id": 2, "printer_id": 2},
            ],
            "dispatched_jobs": [{"job_id": 3, "printer_id": 2}],
            "active_job": {"job_id": 2, "printer_id": 2},
            "recent_event": {"job_id": 2, "printer_id": 2},
        },
    )
    shaped = live_projection.shape(prepared, access("queue:read_own", printers=[1]))["data"]
    assert shaped["processing"] == 1 and shaped["dispatched"] == 0
    assert shaped["active_job"] == {"job_id": 1, "printer_id": 1}
    assert shaped["recent_event"] is None and "total" not in shaped
    assert "hidden" not in json.dumps(shaped) and "secret" not in json.dumps(shaped)


def test_personal_inbox_is_explicit_and_never_global_projection():
    prepared = event("inbox_item", data={"item": {"title": "private"}})
    permissions = access("notifications:inbox")
    assert live_projection.shape(prepared, permissions) is None
    assert live_projection.shape(prepared, permissions, personal=True) == prepared.message


class Browser:
    def __init__(self, blocked=False):
        self.sent, self.closed = [], []
        self.entered, self.released = asyncio.Event(), asyncio.Event()
        if not blocked:
            self.released.set()

    async def accept(self):
        pass

    async def send_text(self, value):
        self.entered.set()
        await self.released.wait()
        self.sent.append(json.loads(value))

    async def close(self, code):
        self.closed.append(code)

    async def receive_json(self):
        await asyncio.Event().wait()


async def attach(manager, browser, permissions, monkeypatch, *, kind="session"):
    handle = LiveHandle(
        LiveDescriptor(
            FrozenAuthority(kind, "account", 7, 0, "sid", key_identity="key" if kind == "key" else None),
            ("websocket:connect",),
        )
    )
    live_authority.handles.add(handle)

    async def admit(value):
        value.admitted = True
        value.proof_until = time.monotonic() + 60
        value.entitlements = permissions

    monkeypatch.setattr(live_authority, "admit", admit)
    await manager.connect(browser, authority=handle.descriptor.capture, live_handle=handle)
    return handle


@pytest.mark.asyncio
async def test_retirement_interrupts_blocked_send_purges_canary_and_ends_silent_receive(monkeypatch):
    manager, browser = ConnectionManager(), Browser(blocked=True)
    handle = await attach(manager, browser, access("printers:read"), monkeypatch)
    await manager.send(browser, {"type": "printer_status", "printer_id": 1, "data": {"progress": 1}})
    await browser.entered.wait()
    await manager.send(browser, {"type": "printer_status", "printer_id": 1, "data": {"progress": 2}})
    receiving = asyncio.create_task(manager.receive(browser))
    handle.retire(4401)
    with pytest.raises(Exception) as error:
        await asyncio.wait_for(receiving, 0.3)
    assert error.value.code == 4401
    await manager.disconnect(browser)
    browser.released.set()
    assert browser.sent == [] and browser.closed == [4401]
    assert handle not in live_authority.handles
    assert not manager._outboxes and not manager._live_handles


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["broken", "timeout"])
async def test_writer_transport_failure_stops_silent_receive_and_drain_owns_actual_close(monkeypatch, failure):
    from starlette.websockets import WebSocketDisconnect

    manager, browser = ConnectionManager(), Browser()
    handle = await attach(manager, browser, access("printers:read"), monkeypatch)
    receive_entered, receive_cancelled = asyncio.Event(), asyncio.Event()
    close_entered, close_release = asyncio.Event(), asyncio.Event()

    async def receive_json():
        receive_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            receive_cancelled.set()

    async def send_text(_value):
        if failure == "broken":
            raise RuntimeError("transport closed")
        await asyncio.Event().wait()

    async def close(code):
        close_entered.set()
        await close_release.wait()
        browser.closed.append(code)

    async def receive_route():
        try:
            with pytest.raises(WebSocketDisconnect) as error:
                await manager.receive(browser)
            assert error.value.code == 1013
        finally:
            await manager.disconnect(browser)

    browser.receive_json, browser.send_text, browser.close = receive_json, send_text, close
    # A real send deadline exercises TimeoutError, rather than faking the
    # exception from send_text. Leave enough time for the held close below.
    if failure == "timeout":
        monkeypatch.setattr("backend.app.core.websocket.SEND_TIMEOUT_SECONDS", 0.1)
    route = live_authority.own(asyncio.create_task(receive_route()))
    await receive_entered.wait()
    await manager.send(browser, {"type": "printer_status", "printer_id": 1, "data": {"progress": 1}})
    await asyncio.wait_for(close_entered.wait(), 1)
    await asyncio.wait_for(receive_cancelled.wait(), 1)
    assert handle.stop.is_set() and handle.retired and not handle.admitted
    assert handle.code == 1013 and handle in live_authority.handles
    writer = manager._outboxes[browser].task
    assert writer in live_authority.cleanups and not writer.done()
    draining = asyncio.create_task(live_authority.drain(timeout=2))
    await asyncio.sleep(0)
    assert not draining.done() and not route.done()
    close_release.set()
    await asyncio.wait_for(route, 1)
    assert await asyncio.wait_for(draining, 1)
    assert browser.closed == [1013] and not manager._live_handles and not manager._outboxes


@pytest.mark.asyncio
async def test_same_class_serializes_once_and_does_not_mutate_shared_payload(monkeypatch):
    manager = ConnectionManager()
    first, second = Browser(), Browser()
    permissions = access("printers:read")
    await attach(manager, first, permissions, monkeypatch)
    await attach(manager, second, permissions, monkeypatch)
    shaped = live_projection.shape
    calls = []

    def counted(*args, **kwargs):
        calls.append(args)
        return shaped(*args, **kwargs)

    monkeypatch.setattr(live_projection, "shape", counted)
    original = {"type": "printer_status", "printer_id": 1, "data": {"progress": 10, "current_print": "hidden"}}
    await manager.broadcast(original)
    await asyncio.sleep(0)
    assert len(calls) == 1 and first.sent == second.sent
    assert first.sent[0]["data"]["current_print"] is None
    assert original["data"]["current_print"] == "hidden"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_accept_is_not_admission_and_revoke_during_accept_never_enqueues(monkeypatch):
    from fastapi import HTTPException

    manager, browser = ConnectionManager(), Browser()
    handle = LiveHandle(LiveDescriptor(FrozenAuthority("session", "account", 7, 0, "sid"), ("websocket:connect",)))
    live_authority.handles.add(handle)
    entered, release = asyncio.Event(), asyncio.Event()

    async def admit(value):
        entered.set()
        await release.wait()
        raise HTTPException(503)

    monkeypatch.setattr(live_authority, "admit", admit)
    connecting = asyncio.create_task(manager.connect(browser, authority=handle.descriptor.capture, live_handle=handle))
    await entered.wait()
    await manager.send(browser, {"type": "printer_status", "printer_id": 1, "data": {"progress": 1}})
    await manager.broadcast({"type": "printer_status", "printer_id": 1, "data": {"progress": 2}})
    assert not browser.sent and not manager._outboxes[browser].messages
    handle.retire(4401)
    release.set()
    with pytest.raises(HTTPException):
        await connecting
    await manager.disconnect(browser)
    assert not browser.sent and browser.closed == [4401]


@pytest.mark.asyncio
async def test_cancelled_disconnect_waiter_keeps_actual_close_and_handle_owned(monkeypatch):
    manager, browser = ConnectionManager(), Browser()
    handle = await attach(manager, browser, access("printers:read"), monkeypatch)
    close_entered, close_release = asyncio.Event(), asyncio.Event()

    async def close(code):
        close_entered.set()
        await close_release.wait()
        browser.closed.append(code)

    browser.close = close
    # Ensure the writer entered before retirement cancellation.
    await asyncio.sleep(0)
    handle.retire(1001)
    await close_entered.wait()
    waiter = asyncio.create_task(manager.disconnect(browser))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert handle in live_authority.handles
    assert manager._outboxes[browser].task in live_authority.cleanups
    actual = manager._disconnects[browser]
    close_release.set()
    await actual
    assert browser.closed == [1001] and handle not in live_authority.handles


@pytest.mark.asyncio
async def test_live_retirement_leaves_peer_producer_and_delivery_running(monkeypatch):
    manager, revoked, peer = ConnectionManager(), Browser(blocked=True), Browser()
    handle = await attach(manager, revoked, access("printers:read"), monkeypatch)
    await attach(manager, peer, access("printers:read"), monkeypatch)
    await manager.broadcast({"type": "printer_status", "printer_id": 1, "data": {"progress": 1}})
    await revoked.entered.wait()
    handle.retire(4401)
    await manager.disconnect(revoked)
    await manager.broadcast({"type": "printer_status", "printer_id": 1, "data": {"progress": 2}})
    await asyncio.sleep(0)
    assert [message["data"]["progress"] for message in peer.sent] == [1, 2]
    assert peer.closed == [] and revoked.sent == []
    await manager.shutdown()


@pytest.mark.asyncio
async def test_writer_expiry_interrupts_before_queued_canary_and_uses_renewable_code(monkeypatch):
    from dataclasses import replace

    manager, browser = ConnectionManager(), Browser(blocked=True)
    handle = await attach(manager, browser, access("printers:read"), monkeypatch)
    await manager.send(browser, {"type": "printer_status", "printer_id": 1, "data": {"progress": 1}})
    await browser.entered.wait()
    await manager.send(browser, {"type": "printer_status", "printer_id": 1, "data": {"progress": 2}})
    handle.descriptor = replace(handle.descriptor, expires=time.monotonic() - 1)
    browser.released.set()
    await manager._outboxes[browser].task
    assert len(browser.sent) == 1 and browser.sent[0]["data"]["progress"] == 1
    assert browser.closed == [1001]
    await manager.disconnect(browser)


@pytest.mark.asyncio
async def test_reused_printer_pk_cannot_deliver_new_incarnation_before_recheck(monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    manager, old, fresh = ConnectionManager(), Browser(), Browser()
    permissions = replace(access("printers:read", printers=[1]), printer_identities=((1, "old-uuid"),))
    printer_manager.remember_record_identity(1, "old-uuid", proven_connection=True)
    handle = await attach(manager, old, permissions, monkeypatch, kind="key")
    await manager.send_printer_status(1, {"progress": 1})
    await asyncio.sleep(0)
    source_snapshot = printer_manager.record_identity_snapshot()
    life = printer_manager.begin_printer_deletion(1)
    assert 1 not in printer_manager.record_identity_snapshot()
    printer_manager.end_printer_deletion(1, life)
    printer_manager.remember_record_identity(1, "new-uuid", proven_connection=True)
    assert source_snapshot[1] == "old-uuid"
    # A different handle's admission cannot replace a proven source identity.
    printer_manager.remember_record_identity(1, "old-uuid")
    assert printer_manager.record_identity_snapshot()[1] == "new-uuid"
    await attach(manager, fresh, replace(permissions, printer_identities=((1, "new-uuid"),)), monkeypatch, kind="key")
    observed = []
    manager.add_internal_listener(observed.append)
    assert handle.deliverable() and not handle.retired  # deliberately before any coordinator tick
    await manager.send_printer_status(1, {"progress": 99})
    await asyncio.sleep(0)
    assert handle.retired and handle.code == 1013
    assert [row["data"]["progress"] for row in old.sent] == [1]
    assert [row["data"]["progress"] for row in fresh.sent] == [99]
    assert observed == [{"type": "printer_status", "printer_id": 1, "data": {"progress": 99}}]
    # This test owns no endpoint receive loop. Run its old channel's route
    # finalizer explicitly even if retirement already completed the writer.
    await manager.disconnect(old)
    await manager.shutdown()
    assert handle not in live_authority.handles
    assert not manager._live_handles and not manager._outboxes


@pytest.mark.asyncio
async def test_event_identity_snapshot_cannot_borrow_a_new_uuid_after_metadata_await(monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    printer_manager.remember_record_identity(1, "old-uuid", proven_connection=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def rows(_refs):
        entered.set()
        await release.wait()
        return {("library", 2): {"id": 2, "created_by_id": 7, "filename": "source"}}

    monkeypatch.setattr(live_authority, "resource_rows", rows)
    pending = asyncio.create_task(
        live_projection.prepare(
            {
                "type": "background_dispatch",
                "data": {"active_jobs": [{"job_id": 1, "printer_id": 1, "kind": "print_library_file", "source_id": 2}]},
            }
        )
    )
    await entered.wait()
    printer_manager.remember_record_identity(1, "new-uuid", proven_connection=True)
    release.set()
    prepared = await pending
    assert prepared.source_printer_identities[1] == "old-uuid"
    new = replace(access("queue:read_all", "library:read_all", printers=[1]), printer_identities=((1, "new-uuid"),))
    with pytest.raises(live_projection.IncarnationChanged):
        live_projection.shape(prepared, new, credential_kind="key")


@pytest.mark.asyncio
async def test_scoped_source_without_proven_identity_resyncs_instead_of_silent_event_loss(monkeypatch):
    manager, browser = ConnectionManager(), Browser()
    permissions = replace(access("printers:read", printers=[1]), printer_identities=((1, "old-uuid"),))
    handle = await attach(manager, browser, permissions, monkeypatch, kind="key")
    from backend.app.services.printer_manager import printer_manager

    life = printer_manager.begin_printer_deletion(1)
    printer_manager.end_printer_deletion(1, life)
    await manager.send_printer_status(1, {"progress": 2})
    assert handle.retired and not browser.sent
    await manager.disconnect(browser)
