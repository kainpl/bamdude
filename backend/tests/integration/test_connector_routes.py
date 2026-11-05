"""The slicer connector's HTTP contract through the real ASGI app (OrcaBamDude spec r2 §7, addendum C2).

- API key only: a browser session is refused with the connector's own code.
- A new operation answers 201, its replay 200 with ``replayed``; content changed under the same id is 409.
- The scope gate refuses a restricted key's AutoQueue entry and a printer outside its list before any handler, and
  reads an existing operation's targets from THIS key's namespace only: a foreign and a missing operation are the
  same 404 -- no 403 tells one from the other.
- The kill switch stops new work and keeps reconciliation; the body is capped; a restore hold refuses admission.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.connector_submission import ConnectorSubmission, ConnectorSubmissionEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.schemas.connector import SubmissionCreate
from backend.app.services import connector_submissions as cs, queue_sources
from backend.app.services.connector_runtime import connector_runtime
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

BASE = "/api/v1/connector"


@pytest.fixture(autouse=True)
def _clean_state():
    queue_sources._reset_state()
    connector_runtime._reset_for_tests()
    yield
    queue_sources._reset_state()
    connector_runtime._reset_for_tests()


async def _key(db_session, **flags) -> tuple[str, APIKey]:
    raw, key_hash, prefix = generate_api_key()
    values = {"can_queue": True, "can_read_status": True, **flags}
    key = APIKey(name=f"orca-{prefix}", key_hash=key_hash, key_prefix=prefix, enabled=True, **values)
    db_session.add(key)
    await db_session.commit()
    await db_session.refresh(key)
    return raw, key


@pytest.fixture
async def farm(db_session, tmp_path, printer_factory, monkeypatch, async_client):
    source, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    raw, key = await _key(db_session)
    caps = await async_client.get(f"{BASE}/capabilities", headers={"X-API-Key": raw})
    assert caps.status_code == 200, caps.text
    return {
        "client": async_client,
        "db": db_session,
        "source": source,
        "sha": hashlib.sha256(Path(source.file_path).read_bytes()).hexdigest(),
        "printer": printer,
        "raw": raw,
        "key": key,
        "caps": caps.json(),
    }


def _entry(farm, **overrides):
    entry = {
        "entry_id": str(uuid.uuid4()),
        "plate_id": 15,
        "quantity": 2,
        "target": {
            "kind": "printer",
            "printer_id": farm["printer"].id,
            "printer_identity": farm["printer"].connector_identity,
        },
        "action": "queue_manual",
    }
    entry.update(overrides)
    return entry


def _body(farm, *entries, **overrides):
    body = {
        "protocol_version": 1,
        "operation_id": str(uuid.uuid4()),
        "server_id": farm["caps"]["connector_server_id"],
        "storage_epoch": farm["caps"]["storage_epoch"],
        "library_file_id": farm["source"].id,
        "source_sha256": farm["sha"],
        "entries": list(entries) or [_entry(farm)],
    }
    body.update(overrides)
    return body


async def _record_only(farm, body):
    """The operation recorded, its coordinator never run -- what a process that died right after leaves."""
    actor = cs.ConnectorActor(key_id=farm["key"].id, key_identity=farm["key"].connector_identity, owner_id=None)
    await cs.register(actor, SubmissionCreate.model_validate(body))


async def _post(farm, body, raw=None):
    return await farm["client"].post(f"{BASE}/submissions", json=body, headers={"X-API-Key": raw or farm["raw"]})


async def _jobs(db_session) -> int:
    return await db_session.scalar(select(func.count()).select_from(PrintQueueItem))


async def _submissions(db_session) -> int:
    return await db_session.scalar(select(func.count()).select_from(ConnectorSubmission))


async def test_capabilities_name_the_key_by_its_identity_and_say_what_it_may_do(farm):
    caps = farm["caps"]

    assert caps["protocol_version"] == 1 and caps["product"] == "bamdude"
    assert caps["actor_id"] == farm["key"].connector_identity
    assert caps["features"]["submissions"] and caps["features"]["auto_target"]
    assert caps["limits"]["max_entries"] == 64 and caps["limits"]["max_concurrent_uploads"] == 2
    assert farm["raw"] not in json.dumps(caps)


async def test_a_browser_session_is_not_a_connector(farm):
    response = await farm["client"].get(f"{BASE}/capabilities")

    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "invalid_key"


async def test_a_new_operation_answers_201_and_its_replay_200(farm):
    body = _body(farm)

    first = await _post(farm, body)
    again = await _post(farm, body)

    assert first.status_code == 201, first.text
    assert first.json()["state"] == "complete" and not first.json()["replayed"]
    assert again.status_code == 200 and again.json()["replayed"]
    assert again.json()["entries"] == first.json()["entries"]
    assert await _jobs(farm["db"]) == 2


async def test_other_content_under_the_same_id_is_a_conflict(farm):
    body = _body(farm)
    await _post(farm, body)

    changed = await _post(farm, {**body, "entries": [_entry(farm, quantity=3)]})

    assert changed.status_code == 409
    assert changed.json()["detail"]["error"] == "operation_conflict"


async def test_an_operation_for_another_epoch_is_refused(farm):
    response = await _post(farm, _body(farm, storage_epoch=str(uuid.uuid4())))

    assert response.status_code == 409 and response.json()["detail"]["error"] == "context_changed"


async def test_an_unknown_field_is_refused_not_dropped(farm):
    body = _body(farm)
    body["entries"][0]["project_id"] = 1

    response = await _post(farm, body)

    assert response.status_code == 422 and response.json()["detail"]["error"] == "invalid_request"
    assert await _submissions(farm["db"]) == 0


async def test_a_body_over_the_cap_is_refused(farm):
    body = _body(farm)
    body["padding"] = "x" * (300 * 1024)

    response = await _post(farm, body)

    assert response.status_code == 413 and response.json()["detail"]["error"] == "request_too_large"


async def test_a_restricted_key_sending_to_the_auto_queue_is_refused_in_the_gate(farm):
    raw, _key_row = await _key(farm["db"], printer_ids=[farm["printer"].id])
    entry = {
        "entry_id": str(uuid.uuid4()),
        "plate_id": 15,
        "target": {"kind": "auto", "target_model": "P1P"},
        "action": "queue_asap",
    }

    response = await _post(farm, _body(farm, _entry(farm), entry), raw)

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "auto_requires_unrestricted_key"
    assert await _submissions(farm["db"]) == 0


async def test_a_restricted_key_naming_another_printer_is_refused_in_the_gate(farm):
    raw, _key_row = await _key(farm["db"], printer_ids=[farm["printer"].id + 50])

    response = await _post(farm, _body(farm), raw)

    assert response.status_code == 403 and response.json()["detail"]["error"] == "printer_scope"
    assert await _submissions(farm["db"]) == 0


@pytest.mark.parametrize("path", ["", "/resume", "/cancel"])
async def test_a_foreign_operation_and_a_missing_one_are_the_same_404(farm, path):
    body = _body(farm)
    await _post(farm, body)
    # A stranger restricted to other printers: a lookup of the foreign manifest would answer 403 here.
    stranger, _row = await _key(farm["db"], printer_ids=[farm["printer"].id + 50])
    method = farm["client"].get if path == "" else farm["client"].post

    foreign = await method(f"{BASE}/submissions/{body['operation_id']}{path}", headers={"X-API-Key": stranger})
    missing = await method(f"{BASE}/submissions/{uuid.uuid4()}{path}", headers={"X-API-Key": stranger})

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["detail"]["error"] == "operation_not_found"


async def test_an_own_operation_whose_printer_left_the_keys_list_is_refused_in_the_gate(farm):
    body = _body(farm)
    await _post(farm, body)
    await farm["db"].execute(
        update(APIKey).where(APIKey.id == farm["key"].id).values(printer_ids=[farm["printer"].id + 50])
    )
    await farm["db"].commit()

    response = await farm["client"].get(
        f"{BASE}/submissions/{body['operation_id']}", headers={"X-API-Key": farm["raw"]}
    )

    assert response.status_code == 403 and response.json()["detail"]["error"] == "printer_scope"


async def test_get_reads_the_receipt(farm):
    body = _body(farm)
    created = await _post(farm, body)

    response = await farm["client"].get(
        f"{BASE}/submissions/{body['operation_id']}", headers={"X-API-Key": farm["raw"]}
    )

    assert response.status_code == 200
    assert response.json()["entries"] == created.json()["entries"]


async def test_resume_runs_an_entry_a_dead_coordinator_left_preparing(farm):
    body = _body(farm)
    # The operation was recorded and its coordinator died inside the entry.
    await _record_only(farm, body)
    await farm["db"].execute(update(ConnectorSubmissionEntry).values(state="preparing"))
    await farm["db"].commit()

    replay = await _post(farm, body)
    assert replay.status_code == 200 and replay.json()["entries"][0]["state"] == "processing"
    assert await _jobs(farm["db"]) == 0

    resumed = await farm["client"].post(
        f"{BASE}/submissions/{body['operation_id']}/resume", headers={"X-API-Key": farm["raw"]}
    )

    assert resumed.status_code == 200 and resumed.json()["state"] == "complete"
    assert await _jobs(farm["db"]) == 2


async def test_cancel_fences_what_was_not_published(farm):
    body = _body(farm)
    await _record_only(farm, body)

    cancelled = await farm["client"].post(
        f"{BASE}/submissions/{body['operation_id']}/cancel", headers={"X-API-Key": farm["raw"]}
    )
    replay = await _post(farm, body)

    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert replay.json()["state"] == "cancelled"
    assert await _jobs(farm["db"]) == 0


async def test_the_kill_switch_stops_new_work_and_keeps_reconciliation(farm):
    body = _body(farm)
    await _post(farm, body)
    farm["db"].add(Settings(key="connector_enabled", value="false"))
    await farm["db"].commit()

    refused = await _post(farm, _body(farm))
    read = await farm["client"].get(f"{BASE}/submissions/{body['operation_id']}", headers={"X-API-Key": farm["raw"]})

    assert refused.status_code == 503 and refused.json()["detail"]["error"] == "connector_disabled"
    assert read.status_code == 200 and read.json()["state"] == "complete"


async def test_a_key_without_queue_rights_cannot_submit(farm):
    raw, _row = await _key(farm["db"], can_queue=False)

    response = await _post(farm, _body(farm), raw)

    assert response.status_code == 403
    assert await _submissions(farm["db"]) == 0


async def test_a_replay_starts_an_entry_that_was_never_attempted(farm):
    body = _body(farm)
    await _record_only(farm, body)

    replay = await _post(farm, body)

    assert replay.status_code == 200 and replay.json()["state"] == "complete"
    assert await _jobs(farm["db"]) == 2


async def test_a_restore_hold_refuses_admission_before_anything_is_recorded(farm):
    connector_runtime._restore_hold = True

    response = await _post(farm, _body(farm))

    assert response.status_code == 503 and response.json()["detail"]["error"] == "storage_restoring"
    assert await _submissions(farm["db"]) == 0


async def test_the_administrator_sets_the_switch_and_the_cap_through_settings(farm):
    client = farm["client"]
    too_big = await client.put("/api/v1/settings/", json={"connector_max_upload_bytes": 600 * 1024 * 1024})
    saved = await client.put(
        "/api/v1/settings/", json={"connector_enabled": False, "connector_max_upload_bytes": 2 * 1024 * 1024}
    )

    assert too_big.status_code == 422
    assert saved.status_code == 200, saved.text
    assert saved.json()["connector_enabled"] is False
    assert saved.json()["connector_max_upload_bytes"] == 2 * 1024 * 1024
    caps = (await client.get(f"{BASE}/capabilities", headers={"X-API-Key": farm["raw"]})).json()
    assert caps["enabled"] is False and not caps["features"]["submissions"]
    assert caps["limits"]["max_upload_bytes"] == 2 * 1024 * 1024
    refused = await _post(farm, _body(farm))
    assert refused.status_code == 503 and refused.json()["detail"]["error"] == "connector_disabled"


async def test_the_printer_list_names_each_printer_by_its_model_code(farm):
    """The connector's device list reads ``model_code`` off ``GET /printers/``; it is the mirrored config's id."""
    printer = farm["printer"]
    printer.model = "P1S"
    await farm["db"].commit()

    response = await farm["client"].get("/api/v1/printers/", headers={"X-API-Key": farm["raw"]})

    assert response.status_code == 200, response.text
    row = next(p for p in response.json() if p["id"] == printer.id)
    assert row["model_code"] == "C12"
    assert row["connector_identity"] == printer.connector_identity
