"""An API key restricted to some printers stays with those printers (audit 1.2.5.3-1.2.5.6, №7).

``APIKey.printer_ids`` was accepted, stored and shown, and enforced by the
webhook routes alone — every other route ignored it, so a key "limited to
printer 1" could read, control, queue for and stop printer 2. It now holds
everywhere a request names a printer:

* **in the path or the query** (``printer_id``, and ``queue_id`` — a printer's
  queue id IS its printer id): checked once, in the permission gate, for every
  route;
* **through queued work** (``/queue/{item_id}``, ``/queue/batch/{batch_id}``):
  the item's printer is checked the same way;
* **in the body** (queueing, moving and bulk-editing jobs): checked by the
  route, which alone can read its body;
* **in lists**: the printers, statuses, queues and queue items a restricted
  key sees are its own.

The auto-queue is farm-wide — its distributor may hand work to any printer — so
a restricted key has no business there at all. ``printer_ids = None`` is every
printer; ``[]`` is none.
"""

from __future__ import annotations

import pytest


@pytest.fixture
async def farm(async_client, db_session, printer_factory, archive_factory):
    """Printers A and B with a queue each, a pending job on each, and three keys."""
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.printer_queues import ensure_printer_queue

    a = await printer_factory(name="A")
    b = await printer_factory(name="B")
    await ensure_printer_queue(db_session, a.id)
    await ensure_printer_queue(db_session, b.id)
    archive = await archive_factory(a.id)
    items = {}
    for printer, position in ((a, 1), (b, 2)):
        item = PrintQueueItem(queue_id=printer.id, archive_id=archive.id, status="pending", position=position)
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)
        items[printer.id] = item

    async def key(printer_ids):
        response = await async_client.post(
            "/api/v1/api-keys/",
            json={"name": f"k{printer_ids}", "can_control_printer": True, "printer_ids": printer_ids},
        )
        assert response.status_code == 200, response.text
        return response.json()["key"]

    keys = {"only_a": await key([a.id]), "all": await key(None), "none": await key([])}
    return a, b, items, archive, keys


def _as(key: str) -> dict:
    return {"X-API-Key": key}


@pytest.mark.asyncio
@pytest.mark.integration
class TestADirectlyNamedPrinter:
    async def test_the_path_names_a_printer_outside_the_list(self, async_client, farm):
        a, b, *_, keys = farm

        allowed = await async_client.get(f"/api/v1/printers/{a.id}/status", headers=_as(keys["only_a"]))
        refused = await async_client.get(f"/api/v1/printers/{b.id}/status", headers=_as(keys["only_a"]))

        assert allowed.status_code == 200, allowed.text
        assert refused.status_code == 403

    async def test_the_query_names_one(self, async_client, farm):
        _a, b, *_, keys = farm

        response = await async_client.get(f"/api/v1/queue/?queue_id={b.id}", headers=_as(keys["only_a"]))

        assert response.status_code == 403

    async def test_a_printers_queue_is_that_printer(self, async_client, farm):
        _a, b, *_, keys = farm

        response = await async_client.get(f"/api/v1/queues/{b.id}", headers=_as(keys["only_a"]))

        assert response.status_code == 403

    async def test_an_empty_list_is_no_printer_at_all(self, async_client, farm):
        a, *_, keys = farm

        response = await async_client.get(f"/api/v1/printers/{a.id}/status", headers=_as(keys["none"]))

        assert response.status_code == 403

    async def test_a_comma_separated_list_names_each_printer(self, async_client, farm):
        """The support bundle attaches the RAW MQTT recording of every printer it is given."""
        a, b, *_, keys = farm

        response = await async_client.get(
            f"/api/v1/support/bundle?mqtt_printer_ids={a.id}, {b.id}", headers=_as(keys["only_a"])
        )

        assert response.status_code == 403

    async def test_an_unrestricted_key_and_a_user_reach_every_printer(self, async_client, farm):
        _a, b, *_, keys = farm

        as_key = await async_client.get(f"/api/v1/printers/{b.id}/status", headers=_as(keys["all"]))
        as_user = await async_client.get(f"/api/v1/printers/{b.id}/status")

        assert (as_key.status_code, as_user.status_code) == (200, 200)


@pytest.mark.asyncio
@pytest.mark.integration
class TestQueuedWork:
    async def test_a_job_on_another_printer_cannot_be_touched(self, async_client, farm):
        a, b, items, _archive, keys = farm

        own = await async_client.get(f"/api/v1/queue/{items[a.id].id}", headers=_as(keys["only_a"]))
        other = await async_client.post(f"/api/v1/queue/{items[b.id].id}/cancel", headers=_as(keys["only_a"]))

        assert own.status_code == 200, own.text
        assert other.status_code == 403

    async def test_work_cannot_be_queued_for_another_printer(self, async_client, farm):
        _a, b, _items, archive, keys = farm

        response = await async_client.post(
            "/api/v1/queue/", json={"queue_id": b.id, "archive_id": archive.id}, headers=_as(keys["only_a"])
        )

        assert response.status_code == 403

    async def test_a_bulk_edit_cannot_reach_another_printers_job(self, async_client, farm):
        a, b, items, _archive, keys = farm

        mixed = await async_client.patch(
            "/api/v1/queue/bulk",
            json={"item_ids": [items[a.id].id, items[b.id].id], "manual_start": True},
            headers=_as(keys["only_a"]),
        )
        moved = await async_client.patch(
            "/api/v1/queue/bulk",
            json={"item_ids": [items[a.id].id], "queue_id": b.id},
            headers=_as(keys["only_a"]),
        )

        assert (mixed.status_code, moved.status_code) == (403, 403)

    async def test_a_job_cannot_be_moved_to_another_printer(self, async_client, farm):
        a, b, items, _archive, keys = farm

        response = await async_client.patch(
            f"/api/v1/queue/{items[a.id].id}", json={"queue_id": b.id}, headers=_as(keys["only_a"])
        )

        assert response.status_code == 403

    async def test_the_farm_wide_auto_queue_is_closed_to_a_restricted_key(self, async_client, farm):
        *_, keys = farm

        restricted = await async_client.get("/api/v1/auto-queue/", headers=_as(keys["only_a"]))
        unrestricted = await async_client.get("/api/v1/auto-queue/", headers=_as(keys["all"]))

        assert (restricted.status_code, unrestricted.status_code) == (403, 200)


@pytest.mark.asyncio
@pytest.mark.integration
class TestABodyThatNamesAPrinter:
    async def test_is_read_on_a_route_that_is_not_the_queue(self, async_client, farm):
        """The gate reads the body, so no route has to remember to."""
        a, b, _items, archive, keys = farm

        own = await async_client.patch(
            f"/api/v1/archives/{archive.id}", json={"printer_id": a.id}, headers=_as(keys["only_a"])
        )
        other = await async_client.patch(
            f"/api/v1/archives/{archive.id}", json={"printer_id": b.id}, headers=_as(keys["only_a"])
        )

        assert (own.status_code, other.status_code) == (200, 403)

    @pytest.mark.parametrize("spelling", [str, float])
    async def test_however_the_id_is_spelled(self, async_client, farm, spelling):
        """Pydantic takes ``"2"`` and ``2.0`` as printer 2, so the scope must too."""
        _a, b, _items, archive, keys = farm

        response = await async_client.post(
            "/api/v1/queue/", json={"queue_id": spelling(b.id), "archive_id": archive.id}, headers=_as(keys["only_a"])
        )

        assert response.status_code == 403

    async def test_a_reference_it_cannot_read_is_refused(self, async_client, farm):
        *_, archive, keys = farm

        response = await async_client.post(
            "/api/v1/queue/", json={"queue_id": "printer B", "archive_id": archive.id}, headers=_as(keys["only_a"])
        )

        assert response.status_code == 403

    async def test_a_reorder_names_its_jobs_by_id(self, async_client, farm):
        _a, b, items, _archive, keys = farm

        response = await async_client.post(
            "/api/v1/queue/reorder",
            json={"items": [{"id": items[b.id].id, "position": 1}]},
            headers=_as(keys["only_a"]),
        )

        assert response.status_code == 403

    async def test_another_printers_job_cannot_be_copied(self, async_client, farm):
        a, b, items, _archive, keys = farm

        response = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": a.id, "source_queue_item_id": items[b.id].id},
            headers=_as(keys["only_a"]),
        )

        assert response.status_code == 403

    async def test_a_batch_that_reaches_another_printer_is_refused(self, async_client, db_session, farm):
        a, b, items, _archive, keys = farm
        items[a.id].batch_id = "mixed"
        items[b.id].batch_id = "mixed"
        await db_session.commit()

        response = await async_client.post("/api/v1/queue/batch/mixed/cancel", headers=_as(keys["only_a"]))

        assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
class TestWhatBelongsToAPrinter:
    async def test_a_smart_plug_is_its_printers(self, async_client, farm, smart_plug_factory):
        a, b, *_, keys = farm
        plug_a = await smart_plug_factory(name="Plug A", printer_id=a.id, ip_address="192.168.1.101")
        plug_b = await smart_plug_factory(name="Plug B", printer_id=b.id, ip_address="192.168.1.102")

        own = await async_client.get(f"/api/v1/smart-plugs/{plug_a.id}", headers=_as(keys["only_a"]))
        other = await async_client.get(f"/api/v1/smart-plugs/{plug_b.id}", headers=_as(keys["only_a"]))

        assert (own.status_code, other.status_code) == (200, 403)

    async def test_a_maintenance_item_is_its_printers(self, async_client, db_session, farm):
        from backend.app.models.maintenance import MaintenanceType, PrinterMaintenance

        a, b, *_, keys = farm
        kind = MaintenanceType(name="Lubricate rods", default_interval_hours=50)
        db_session.add(kind)
        await db_session.commit()
        rows = {pid: PrinterMaintenance(printer_id=pid, maintenance_type_id=kind.id) for pid in (a.id, b.id)}
        db_session.add_all(rows.values())
        await db_session.commit()

        own = await async_client.get(f"/api/v1/maintenance/items/{rows[a.id].id}/history", headers=_as(keys["only_a"]))
        other = await async_client.get(
            f"/api/v1/maintenance/items/{rows[b.id].id}/history", headers=_as(keys["only_a"])
        )

        assert (own.status_code, other.status_code) == (200, 403)

    async def test_a_dispatch_job_is_its_printers(self, async_client, farm, monkeypatch):
        from backend.app.services.background_dispatch import background_dispatch

        _a, b, *_, keys = farm
        monkeypatch.setattr(background_dispatch, "printer_of_job", lambda job_id: b.id)

        response = await async_client.delete("/api/v1/background-dispatch/7", headers=_as(keys["only_a"]))

        assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_bearer_key_is_held_the_same(async_client, farm):
    _a, b, *_, keys = farm

    response = await async_client.get(
        f"/api/v1/printers/{b.id}/status", headers={"Authorization": f"Bearer {keys['only_a']}"}
    )

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
class TestLists:
    async def test_the_monitor_shows_its_own_printers(self, async_client, farm):
        a, *_, keys = farm

        response = await async_client.get("/api/v1/monitor/snapshot?view=printers", headers=_as(keys["only_a"]))

        assert response.status_code == 200, response.text
        assert [p["printer_id"] for p in response.json()["printers"]] == [a.id]

    async def test_a_restricted_key_lists_its_own_printers(self, async_client, farm):
        a, b, *_, keys = farm

        restricted = await async_client.get("/api/v1/printers/", headers=_as(keys["only_a"]))
        unrestricted = await async_client.get("/api/v1/printers/", headers=_as(keys["all"]))

        assert {p["id"] for p in restricted.json()} == {a.id}
        assert {p["id"] for p in unrestricted.json()} >= {a.id, b.id}

    async def test_and_their_statuses(self, async_client, farm):
        a, b, *_, keys = farm

        response = await async_client.get(
            f"/api/v1/printers/status/batch?ids={a.id}&ids={b.id}", headers=_as(keys["only_a"])
        )

        assert response.status_code == 200, response.text
        assert set(response.json()) == {str(a.id)}

    async def test_and_their_queues(self, async_client, farm):
        a, *_, keys = farm

        response = await async_client.get("/api/v1/queues/", headers=_as(keys["only_a"]))

        assert {q["id"] for q in response.json()} == {a.id}

    async def test_and_their_jobs(self, async_client, farm):
        a, _b, items, _archive, keys = farm

        response = await async_client.get("/api/v1/queue/", headers=_as(keys["only_a"]))

        assert {i["id"] for i in response.json()} == {items[a.id].id}

    async def test_and_their_scheduled_dryings(self, async_client, db_session, farm):
        from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying

        a, b, *_, keys = farm
        for printer in (a, b):
            db_session.add(ScheduledDrying(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8))
            db_session.add(
                DryingSchedule(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, start_time="01:00")
            )
        await db_session.commit()

        runs = await async_client.get("/api/v1/scheduled-dryings", headers=_as(keys["only_a"]))
        rules = await async_client.get("/api/v1/drying-schedules", headers=_as(keys["only_a"]))

        assert runs.status_code == 200, runs.text
        assert {r["printer_id"] for r in runs.json()} == {a.id}
        assert {r["printer_id"] for r in rules.json()["schedules"]} == {a.id}
