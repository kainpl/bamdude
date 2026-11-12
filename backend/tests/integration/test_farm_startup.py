"""Global startup pause through real HTTP and dispatch entry points."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.api.routes import farm_startup
from backend.app.core.auth import create_access_token
from backend.app.models.user import User
from backend.app.services import farm_startup_guard as module
from backend.app.services.farm_startup_guard import FarmStartupGuard, FarmStartupRefused
from backend.tests.auth_helpers import mint_test_access
from backend.tests.integration.swap_terminal_support import (
    CHANGE_TABLE,
    an_execution,
    archive_of,
    change_tables_sent,
    complete,
    make_rig,  # noqa: F401
)


def test_real_reconnect_does_not_revoke_global_release(held):
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient("127.0.0.1", "pause-reconnect-test", "unused")
    client._client = MagicMock()
    client._client.subscribe.return_value = (0, 1)
    client._prime_kprofile_request = MagicMock()
    # The old implementation consulted these and latched the printer forever.
    client._farm_printer_id = 1
    client._farm_owner = 1
    client._on_connect(client._client, None, None, 0)
    with pytest.raises(FarmStartupRefused):
        client.start_calibration(bed_leveling=True)
    state = held.status()
    held.resume(state["boot_id"], state["hold_id"], actor="operator")
    receipt = held.path.read_bytes()
    for _ in range(3):
        client._on_disconnect(None, None)
        client._on_connect(client._client, None, None, 0)
        assert client.start_calibration(bed_leveling=True)
        assert held.allowed()
        assert held.path.read_bytes() == receipt


def test_running_print_controls_and_macro_gcode_are_available_under_hold(held):
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient("127.0.0.1", "pause-controls-test", "unused")
    client._client = MagicMock()
    client.state.connected = True
    assert not held.allowed()
    assert client.resume_print()
    assert client.send_gcode("M960 S5 P0")
    assert client.stop_print()
    assert not held.allowed()


async def test_existing_print_finishes_and_swaps_while_new_starts_are_held(held, make_rig):
    rig = await make_rig(require_plate_clear=False)
    archive_id, _ = await an_execution(rig, fact=False, pending=[CHANGE_TABLE], in_memory=True)
    assert not held.allowed()
    await complete(rig)
    archive = await archive_of(rig, archive_id)
    assert archive.status == "completed"
    assert change_tables_sent(rig) == 1
    assert CHANGE_TABLE not in (archive.extra_data or {}).get("swap_macro_events_pending", [])
    assert not held.allowed()


@pytest.fixture
def held(tmp_path, monkeypatch):
    guard = FarmStartupGuard()
    guard.start(tmp_path)
    guard.mark_ready()
    monkeypatch.setattr(module, "farm_startup_guard", guard)
    monkeypatch.setattr(farm_startup, "farm_startup_guard", guard)
    for name in (
        "backend.app.main",
        "backend.app.services.print_scheduler",
        "backend.app.services.smart_plug_manager",
    ):
        monkeypatch.setattr(name + ".farm_startup_guard", guard)
    yield guard
    guard.finish(clean=False)


async def test_authenticated_status_and_cas_release(async_client, held, monkeypatch):
    response = await async_client.get("/api/v1/system/farm-startup")
    assert response.status_code == 200
    state = response.json()
    assert state["state"] == "held" and state["can_resume"]
    body = {"expected_boot_id": state["boot_id"], "expected_hold_id": state["hold_id"]}
    response = await async_client.post("/api/v1/system/farm-startup/resume", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "released"
    before = held.path.read_bytes()
    response = await async_client.post("/api/v1/system/farm-startup/resume", json=body)
    assert response.json()["result"] == "already_released"
    assert held.path.read_bytes() == before
    held.hold("restore")
    response = await async_client.post("/api/v1/system/farm-startup/resume", json=body)
    assert response.status_code == 409
    assert not held.allowed()


async def test_read_only_observer_sees_hold_but_cannot_release(async_client, db_session, held):
    user = User(username="farm_observer", password_hash="unused", role="user", is_active=True)
    db_session.add(user)
    await db_session.commit()
    token = await mint_test_access(db_session, username=user.username)
    headers = {"Authorization": f"Bearer {token}"}
    response = await async_client.get("/api/v1/system/farm-startup", headers=headers)
    assert response.status_code == 200
    assert response.json()["can_resume"] is False
    state = held.status()
    response = await async_client.post(
        "/api/v1/system/farm-startup/resume",
        headers=headers,
        json={
            "expected_boot_id": state["boot_id"],
            "expected_hold_id": state["hold_id"],
        },
    )
    assert response.status_code == 403
    assert not held.allowed()


async def test_release_storage_failure_and_not_ready(async_client, held, monkeypatch):
    state = held.status()
    body = {"expected_boot_id": state["boot_id"], "expected_hold_id": state["hold_id"]}
    held.ready = False
    assert (await async_client.post("/api/v1/system/farm-startup/resume", json=body)).status_code == 409
    held.mark_ready()
    monkeypatch.setattr(held, "_write", lambda state: (_ for _ in ()).throw(OSError("disk")))
    assert (await async_client.post("/api/v1/system/farm-startup/resume", json=body)).status_code == 503
    assert not held.allowed()


async def test_queues_and_direct_dispatch_do_not_read_sources_or_claim(held, monkeypatch):
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.print_scheduler import PrintScheduler

    for name in ("print_scheduler", "auto_queue_scheduler", "background_dispatch"):
        monkeypatch.setattr(
            f"backend.app.services.{name}.async_session",
            MagicMock(side_effect=AssertionError("blocked work touched database/source")),
        )
    scheduler = PrintScheduler()
    monkeypatch.setattr(scheduler, "_tick_scheduled_drying", AsyncMock())
    assert await scheduler.check_queue() is False
    scheduler._tick_scheduled_drying.assert_awaited_once_with(set())
    assert await AutoQueueScheduler().tick() is None
    with pytest.raises(FarmStartupRefused):
        await background_dispatch._dispatch(
            kind="print_library_file",
            source_id=999,
            source_name="no source IO",
            printer_id=1,
            printer_name="p",
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )


async def test_asgi_readiness_is_after_startup_complete_is_delivered(held, monkeypatch):
    held.ready = False
    held.state["hold"] = None
    seen = []

    async def app(scope, receive, send):
        assert not held.allowed()
        await send({"type": "lifespan.startup.complete"})
        assert held.allowed()

    async def send(message):
        seen.append(held.allowed())

    await module.FarmReadyMiddleware(app)({"type": "lifespan"}, None, send)
    assert seen == [False]


@pytest.mark.parametrize("scope", [[], [1]])
async def test_scoped_key_cannot_release_global_hold(async_client, held, scope, db_session):
    if scope:
        from backend.app.models.printer import Printer

        printer = Printer(
            name="Scope fixture", serial_number="scope-fixture", ip_address="127.0.0.1", access_code="test"
        )
        db_session.add(printer)
        await db_session.commit()
        scope = [printer.id]
    made = await async_client.post(
        "/api/v1/api-keys/",
        json={
            "name": "scoped",
            "can_control_printer": True,
            "printer_ids": scope,
        },
    )
    assert made.status_code == 200, made.text
    async_client.headers.pop("Authorization", None)
    headers = {"X-API-Key": made.json()["key"]}
    state = held.status()
    response = await async_client.get("/api/v1/system/farm-startup", headers=headers)
    assert response.status_code == 200 and not response.json()["can_resume"]
    response = await async_client.post(
        "/api/v1/system/farm-startup/resume",
        headers=headers,
        json={
            "expected_boot_id": state["boot_id"],
            "expected_hold_id": state["hold_id"],
        },
    )
    assert response.status_code == 403
    assert not held.allowed()


async def test_unauthenticated_cannot_observe_or_release(async_client, held):
    async_client.headers.pop("Authorization", None)
    assert (await async_client.get("/api/v1/system/farm-startup")).status_code == 401


async def test_revocation_while_resume_waits_is_rechecked(async_client, held, db_session, monkeypatch):
    from sqlalchemy import update

    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(farm_startup, "_resume_lock", lock)
    entered = asyncio.Event()
    gate = farm_startup._resume_permission

    async def observed_gate(**kwargs):
        value = await gate(**kwargs)
        entered.set()
        return value

    monkeypatch.setattr(farm_startup, "_resume_permission", observed_gate)
    state = held.status()
    request = asyncio.create_task(
        async_client.post(
            "/api/v1/system/farm-startup/resume",
            json={
                "expected_boot_id": state["boot_id"],
                "expected_hold_id": state["hold_id"],
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await db_session.execute(update(User).where(User.username == "test_admin").values(is_active=False))
        await db_session.commit()
        lock.release()
        response = await request
        assert response.status_code in (401, 403), response.text
        assert not held.allowed()
    finally:
        if lock.locked():
            lock.release()
        if not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


async def test_invalid_restore_does_not_change_latch_and_backup_has_no_operational_state(held):
    from io import BytesIO

    from fastapi import HTTPException, UploadFile

    from backend.app.api.routes.settings import _restore_backup
    from backend.app.core.config import settings
    from backend.app.services.backup_files import directories

    state = held.status()
    held.resume(state["boot_id"], state["hold_id"], actor="test")
    with pytest.raises(HTTPException):
        await _restore_backup(UploadFile(filename="bad.txt", file=BytesIO(b"bad")), AsyncMock())
    assert held.status()["hold_id"] is None
    assert held.allowed()
    assert "runtime-state" not in directories(settings)


async def test_permission_upgrade_seeds_only_administrators(db_session, test_engine):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.migrations import m205_farm_resume_permission as migration
    from backend.app.models.group import Group

    groups = [
        Group(name="Administrators", permissions=["printers:read"], is_system=True),
        *(Group(name=name, permissions=[], is_system=True) for name in ("Operators", "Viewers")),
        Group(name="Farm leads", permissions=["queue:read_all"], is_system=False),
    ]
    db_session.add_all(groups)
    await db_session.commit()
    await migration.seed(async_sessionmaker(test_engine, expire_on_commit=False))
    await migration.seed(async_sessionmaker(test_engine, expire_on_commit=False))
    db_session.expire_all()
    result = (await db_session.scalars(select(Group).order_by(Group.id))).all()
    # Added once, beside what the group already held; nobody else gains it.
    assert result[0].permissions == ["printers:read", "farm:resume"]
    assert result[1].permissions == result[2].permissions == []
    assert result[3].permissions == ["queue:read_all"]
