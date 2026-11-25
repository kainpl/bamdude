"""P-ID spec T6: a deleted printer's number goes to nobody — through the real routes.

* Create and hard-delete through the API: the next printer gets another number, and with it
  neither the deleted printer's AMS position name nor its process memory.
* PID-S23: an order plan's saved target outlives the printer it names. The newest printer is
  deleted BEFORE m262 with a counter that remembers nothing (as on every SQLite install before
  P-ID); m262's registry alone keeps the number, so "discard changes" restores a target that
  stays unavailable instead of pointing at the next printer.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text

from backend.app.core import printer_id_bound
from backend.app.migrations import _run_sqlite_self_managed, m262_printer_id_never_reissued as m262
from backend.app.models.order_working_plan import OrderWorkingPlan
from backend.app.models.printer import Printer
from backend.tests.integration.test_order_plan_submission import prepared
from backend.tests.integration.test_order_working_plan import save_baseline
from backend.tests.integration.test_orders_api import catalog  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _body(serial: str) -> dict:
    return {
        "name": serial,
        "serial_number": serial,
        "ip_address": "192.0.2.20",
        "access_code": "12345678",
        "model": "X1C",
    }


async def test_a_hard_deleted_printers_number_name_and_memory_go_to_nobody(async_client):
    from backend.app.services import firmware_update

    probe = AsyncMock(return_value={"success": True, "state": "IDLE", "model": "X1C"})
    # No real MQTT client: an active printer created over HTTP is connected right away, and a paho thread
    # left running would outlive this test (acceptance PID-A04).
    connected: list[int] = []
    connect = AsyncMock(side_effect=lambda printer, *_a, **_k: connected.append(printer.id) or True)
    with (
        patch("backend.app.api.routes.printers.printer_manager.test_connection", new=probe),
        patch("backend.app.api.routes.printers.printer_manager.connect_printer", new=connect),
    ):
        first = (await async_client.post("/api/v1/printers/", json=_body("00M09A000000101"))).json()["id"]
        old = (await async_client.post("/api/v1/printers/", json=_body("00M09A000000102"))).json()["id"]
        named = await async_client.put(f"/api/v1/printers/{old}/ams-labels/0", json={"label": "Left rack"})
        assert named.status_code == 200, named.text
        firmware_update._upload_states[old] = MagicMock()  # memory the delete does not clear (spec §D)
        try:
            assert (await async_client.delete(f"/api/v1/printers/{old}")).status_code == 200
            new = (await async_client.post("/api/v1/printers/", json=_body("00M09A000000103"))).json()["id"]
            assert first < old < new

            status = MagicMock(raw_data={"ams": [{"id": 0}]})
            with patch("backend.app.api.routes.printers.printer_manager.get_status", return_value=status):
                labels = await async_client.get(f"/api/v1/printers/{new}/ams-labels")
            assert labels.status_code == 200 and labels.json() == {}
            assert new not in firmware_update._upload_states
            assert connected == [first, old, new]
        finally:
            firmware_update._upload_states.pop(old, None)


@pytest.mark.parametrize("registry_reads_saved_entries", [True, False], ids=["registry", "without-the-carrier"])
async def test_a_saved_plan_target_stays_unavailable_after_the_number_would_have_come_back(
    registry_reads_saved_entries, committing_client, db_session, catalog, test_engine, monkeypatch
):
    path, entry, state = await prepared(committing_client, db_session, catalog)
    deleted = Printer(
        name="Retired", serial_number="PID-OLD", ip_address="192.0.2.12", access_code="00000000", model="P1S"
    )
    db_session.add(deleted)
    await db_session.commit()
    old_id = deleted.id

    pinned = {**entry, "target": {"kind": "printer", "printer_id": old_id}}
    edit = await committing_client.put(path, json={"expected_revision": state["revision"], "entries": [pinned]})
    assert edit.status_code == 200 and edit.json()["issues"] == [], edit.text
    order_id = int(path.split("/")[-2])
    saved = await save_baseline(committing_client, order_id, edit.json())
    draft = await committing_client.put(path, json={"expected_revision": saved["revision"], "entries": [entry]})
    assert draft.status_code == 200, draft.text

    # Delete the newest printer before P-ID, with a counter that remembers nothing — a SQLite
    # install before m262 had no counter at all, and max(id) + 1 handed the number out again.
    await db_session.delete(deleted)
    await db_session.commit()
    await db_session.execute(text("DELETE FROM sqlite_sequence WHERE name = 'printers'"))
    await db_session.commit()
    plan = await db_session.scalar(select(OrderWorkingPlan).where(OrderWorkingPlan.id == order_id))
    assert plan.entries[0]["target"]["kind"] == "auto"
    assert plan.saved_entries[0]["target"]["printer_id"] == old_id  # the only place the number lives on
    await db_session.commit()

    if not registry_reads_saved_entries:
        carriers = tuple(c for c in printer_id_bound.CARRIERS if c.column != "saved_entries")
        monkeypatch.setattr(printer_id_bound, "CARRIERS", carriers)
    await _run_sqlite_self_managed(test_engine, m262)

    replacement = Printer(
        name="Replacement", serial_number="PID-NEW", ip_address="192.0.2.13", access_code="00000000", model="P1S"
    )
    db_session.add(replacement)
    await db_session.commit()
    new_id = replacement.id

    updated = draft.json()
    discard = await committing_client.post(
        path + "/discard",
        json={
            "expected_revision": updated["revision"],
            "expected_saved_revision": updated["saved_revision"],
            "expected_epoch": updated["epoch"],
        },
    )
    assert discard.status_code == 200, discard.text
    restored = discard.json()
    assert restored["entries"][0]["target"]["printer_id"] == old_id
    issues = [issue["code"] for issue in restored["issues"]]
    registration = await committing_client.post(
        path + "/submissions",
        json={
            "operation_id": "pid-t6",
            "expected_revision": restored["revision"],
            "parts": [{"entry_id": entry["entry_id"], "count": 1}],
        },
    )
    if registry_reads_saved_entries:
        assert new_id == old_id + 1
        assert issues == ["target_unavailable"]
        assert registration.status_code == 409 and "target_unavailable" in registration.text
    else:  # the failure P-ID closes: the old target is a valid target for the next printer
        assert new_id == old_id
        assert issues == []
        assert registration.status_code == 200, registration.text
