"""Addressed recovery surfaces: no farm/real target, actual child CLI and source files."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, UploadFile
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.app.core import schema_repair_maintenance as maintenance
from backend.app.core.schema_restore_consent import ConsentReceipts
from backend.app.services.render_runtime import _try_lock, _unlock

ROOT = Path(__file__).resolve().parents[4]


def cli(root, operation, **extra):
    env = {
        **os.environ,
        "BAMDUDE_IGNORE_DOTENV": "1",
        "DATA_DIR": str(root),
        "DATABASE_URL": "",
        "LOG_DIR": str(root / "must-not-be-created"),
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-m", "backend.app.cli", "schema_repair", operation],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )


def files(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != "owner.lock"
    }


def test_status_creates_no_database_directory_or_embedded_secrets(tmp_path):
    root = tmp_path / "absent"
    result = cli(root, "status")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["database_present"] is False
    assert not root.exists()
    result = cli(root, "status", DATABASE_URL="embedded")
    assert result.returncode == 2
    assert not root.exists()


def test_status_reads_wal_copy_without_changing_original_files(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    with closing(sqlite3.connect(root / "bamdude.db")) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript('CREATE TABLE settings(key TEXT,value TEXT); INSERT INTO settings VALUES("visible","safe");')
        db.commit()
        before = files(root)
        result = cli(root, "status")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "debt": False,
            "automatic_attempts_remaining": None,
            "last_failure": None,
            "residual_objects": [],
        }
        assert files(root) == before


def test_status_reports_safe_failure_class_without_initializing_application(tmp_path):
    with closing(sqlite3.connect(tmp_path / "bamdude.db")) as db:
        db.execute("CREATE TABLE settings(key TEXT,value TEXT)")
        db.execute(
            "INSERT INTO settings VALUES(?,?)",
            (
                "_bamdude.schema_repair_debt",
                json.dumps(
                    {
                        "version": 1,
                        "automatic_attempts_remaining": 0,
                        "last_failure": "technical_repair_failure",
                    }
                ),
            ),
        )
        db.commit()
    before = files(tmp_path)
    result = cli(tmp_path, "status")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["last_failure"] == "technical_repair_failure"
    assert files(tmp_path) == before


@pytest.mark.parametrize("operation", ["status", "retry"])
def test_cli_refuses_held_installation_lock_without_starting_anything(tmp_path, operation):
    path = tmp_path / "runtime-state/farm-startup/owner.lock"
    path.parent.mkdir(parents=True)
    with path.open("a+b") as handle:
        assert _try_lock(handle)
        try:
            before = files(tmp_path)
            result = cli(tmp_path, operation)
            assert result.returncode == 2
            assert files(tmp_path) == before
        finally:
            _unlock(handle)


def test_cli_has_no_force_or_clear(tmp_path):
    for operation in ("clear", "force", "retry --force"):
        assert cli(tmp_path, operation).returncode == 2
    assert not (tmp_path / "bamdude.db").exists()


def test_maintenance_is_outer_to_auth_and_data_and_closes_websockets(monkeypatch):
    entered = []

    async def route(request):
        entered.append(request.url.path)
        return JSONResponse({"secret": "never visible in maintenance"})

    app = Starlette(routes=[Route("/health", route), Route("/api/v1/auth/status", route), Route("/", route)])
    app.add_middleware(maintenance.SchemaRepairMaintenanceMiddleware)
    monkeypatch.setattr(maintenance, "active", True)
    with TestClient(app) as client:
        for path in ("/health", "/api/v1/auth/status", "/", "/api/v1/printers"):
            response = client.get(path)
            assert response.status_code == 503
            assert response.json() == {"status": "maintenance", "reason": "schema_repair_pending"}
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as caught, client.websocket_connect("/api/v1/ws"):
            pytest.fail("maintenance admitted a websocket")
        assert caught.value.code == 1013
    assert entered == []


@pytest.mark.parametrize("field", ["actor", "zip", "source", "target", "count"])
def test_receipt_is_bound_to_every_decision_input_and_cannot_be_replayed(field):
    owner = ConsentReceipts()
    expected = {
        "actor": {"kind": "session", "generation": 3},
        "zip": "one",
        "source": "one",
        "target": "one",
        "count": 2,
    }
    token = owner.issue(expected)
    changed = {**expected, field: "changed"}
    assert owner.consume(token, changed) is None
    assert owner.consume(token, expected) is None
    token = owner.issue(expected)
    assert owner.consume(token, expected)["count"] == 2
    assert owner.consume(token, expected) is None


def test_receipt_expiry_requires_fresh_decision(monkeypatch):
    from backend.app.core import schema_restore_consent as consent

    clock = [100.0]
    monkeypatch.setattr(consent.time, "monotonic", lambda: clock[0])
    owner = ConsentReceipts()
    expected = {"actor": {"kind": "session"}}
    token = owner.issue(expected)
    clock[0] += 301
    assert owner.consume(token, expected) is None


@pytest.mark.asyncio
async def test_http_calibration_confirmation_happens_before_hold_drain_or_replacement(tmp_path, monkeypatch):
    from backend.app.api.routes import settings as route
    from backend.app.core.schema_index_repair import CALIBRATION, manifest
    from backend.app.services.backup_files import FileRestore
    from backend.app.services.farm_startup_guard import farm_startup_guard

    row = next(r for r in manifest() if r["name"] == CALIBRATION)
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.executescript("CREATE TABLE printers(id INTEGER); CREATE TABLE settings(key TEXT,value TEXT);")
        names = ",".join('"' + c + '"' for c in row["columns"] + ["is_active"])
        db.execute(f"CREATE TABLE filament_calibration({names})")
        db.execute("INSERT INTO filament_calibration VALUES(1,1,1,1,1,1),(1,1,1,1,1,1)")
        db.commit()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.write(source, "bamdude.db")
    stream.seek(0)
    hold = Mock()
    prepare = Mock()
    drain = AsyncMock()
    monkeypatch.setattr(farm_startup_guard, "hold", hold)
    monkeypatch.setattr(FileRestore, "prepare", prepare)
    monkeypatch.setattr(route, "_quiesce_part_render", drain)
    monkeypatch.setattr(
        "backend.app.services.connector_submissions.read_server_identity", AsyncMock(return_value=("server", "epoch"))
    )
    with pytest.raises(HTTPException) as caught:
        await route._restore_backup(UploadFile(file=stream, filename="backup.zip"), AsyncMock())
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "calibration_restore_confirmation_required"
    assert caught.value.detail["duplicate_groups"] == 1
    hold.assert_not_called()
    prepare.assert_not_called()
    drain.assert_not_awaited()


@pytest.mark.parametrize("case", ["unknown-index", "stale-confirmation"])
async def test_structural_or_obsolete_confirmation_refusal_precedes_hold(tmp_path, monkeypatch, case):
    from backend.app.api.routes import settings as route
    from backend.app.services.backup_files import FileRestore
    from backend.app.services.farm_startup_guard import farm_startup_guard

    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.executescript("CREATE TABLE printers(id INTEGER); CREATE TABLE settings(key TEXT,value TEXT);")
        if case == "unknown-index":
            db.executescript(
                "CREATE TABLE auto_queue_items(batch_id TEXT);"
                "CREATE INDEX ix_auto_queue_batch_id ON auto_queue_items(batch_id DESC);"
            )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.write(source, "bamdude.db")
    stream.seek(0)
    hold, prepare = Mock(), Mock()
    monkeypatch.setattr(farm_startup_guard, "hold", hold)
    monkeypatch.setattr(FileRestore, "prepare", prepare)
    with pytest.raises(HTTPException) as caught:
        await route._restore_backup(
            UploadFile(file=stream, filename="backup.zip"),
            AsyncMock(),
            accept_calibration_residual=case == "stale-confirmation",
            calibration_receipt="stale",
        )
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == (
        "sqlite_source_index_incompatible" if case == "unknown-index" else "calibration_restore_confirmation_invalid"
    )
    hold.assert_not_called()
    prepare.assert_not_called()
