"""An order's prints and queue rows as a reader of the order sees them (WS-13 E13 T15, O12).

``orders:read`` opens the order — not the archive section or the queue. A print the caller may
not read as an archive comes back as a minimal row (what the order page shows of it: name,
status, quantity, defects, its line); a queue row the caller may not read as a queue row loses
its operator, its slots and its settings; and a library file's name — in the queue rows and the
timeline — shows only to a caller the library would show it to (as ``/plan`` already does).
"""

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.project import Project
from backend.app.services.printer_queues import ensure_printer_queue
from backend.tests.integration.test_workshop_library_rights import _jwt, _user

pytestmark = pytest.mark.integration


@pytest.fixture
async def desk(db_session, printer_factory):
    printer = await printer_factory(name="P")
    await ensure_printer_queue(db_session, printer.id)
    owner = await _user(db_session, "proj_owner", ["library:read_own", "queue:read_own", "archives:read_own"])
    order = Project(name="Projection order")
    db_session.add(order)
    await db_session.flush()
    secret = LibraryFile(
        filename="secret-design.gcode.3mf", file_path="", file_type="3mf", file_size=0, created_by_id=owner.id
    )
    db_session.add(secret)
    await db_session.flush()
    archive = PrintArchive(
        project_id=order.id,
        filename="secret-design.gcode.3mf",
        print_name="Shade",
        file_path="archive/1/secret.3mf",
        file_size=1,
        quantity=2,
        status="completed",
        notes="supplier's private remark",
        created_by_id=owner.id,
    )
    row = PrintQueueItem(
        queue_id=printer.id,
        project_id=order.id,
        library_file_id=secret.id,
        status="pending",
        position=1,
        ams_mapping="[2, -1]",
        created_by_id=owner.id,
    )
    db_session.add_all([archive, row])
    await db_session.commit()
    return {"order": order.id, "archive": archive.id, "row": row.id}


@pytest.mark.asyncio
async def test_a_reader_of_the_order_sees_its_prints_as_minimal_rows(async_client, db_session, desk):
    await _user(db_session, "proj_reader", ["orders:read"])
    await _user(db_session, "proj_archivist", ["orders:read", "archives:read_all"])

    [row] = (
        await async_client.get(f"/api/v1/projects/{desk['order']}/archives", headers=await _jwt("proj_reader"))
    ).json()
    assert row["id"] == desk["archive"]
    assert row["restricted"] is True
    assert row["print_name"] == "Shade"
    assert row["quantity"] == 2
    assert "notes" not in row
    assert "file_path" not in row

    [full] = (
        await async_client.get(f"/api/v1/projects/{desk['order']}/archives", headers=await _jwt("proj_archivist"))
    ).json()
    assert full["notes"] == "supplier's private remark"
    assert not full.get("restricted")


@pytest.mark.asyncio
async def test_a_reader_of_the_order_sees_its_queue_rows_without_the_queues_details(async_client, db_session, desk):
    await _user(db_session, "proj_q_reader", ["orders:read"])
    await _user(db_session, "proj_q_full", ["orders:read", "queue:read_all", "library:read_all"])

    [plain] = (
        await async_client.get(f"/api/v1/projects/{desk['order']}/queue", headers=await _jwt("proj_q_reader"))
    ).json()["pending"]
    assert plain["id"] == desk["row"]
    assert plain["library_file_name"] is None
    assert plain["created_by_username"] is None
    assert plain["ams_mapping"] is None

    [full] = (
        await async_client.get(f"/api/v1/projects/{desk['order']}/queue", headers=await _jwt("proj_q_full"))
    ).json()["pending"]
    assert full["library_file_name"] == "secret-design.gcode.3mf"
    assert full["created_by_username"] == "proj_owner"
    assert full["ams_mapping"] == [2, -1]


@pytest.mark.asyncio
async def test_the_timeline_names_a_library_file_only_to_whom_the_library_shows_it(async_client, db_session, desk):
    await _user(db_session, "proj_t_reader", ["orders:read"])
    await _user(db_session, "proj_t_lib", ["orders:read", "library:read_all"])

    def queued(events):
        return [e for e in events if (e.get("metadata") or {}).get("queue_item_id") == desk["row"]]

    [hidden] = queued(
        (
            await async_client.get(f"/api/v1/projects/{desk['order']}/timeline", headers=await _jwt("proj_t_reader"))
        ).json()
    )
    assert "secret-design" not in (hidden.get("description") or "")
    [shown] = queued(
        (await async_client.get(f"/api/v1/projects/{desk['order']}/timeline", headers=await _jwt("proj_t_lib"))).json()
    )
    assert "secret-design" in shown["description"]
