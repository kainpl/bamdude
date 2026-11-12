"""Filing a past print — one policy on every door (WS-13 E13 T16, O08/O09/O21).

``F(print)`` = ``orders:file_prints`` (any print) OR ``orders:update`` with the archive's own
update right (own with ``update_own``, any with ``update_all``). A NEW link goes only to an
active order (409 ``order_closed``); a print leaves an order by C1 (409 when the order already
received its output). The archive editor's PATCH keeps the archive's own right for its fields,
and a binding that changes asks ``orders:update`` or ``orders:file_prints`` beside it, an order
that exists and is open, a line of it and C1; ``status`` is a closed set, and a filed print
leaving ``completed`` is C1 too. A line change inside one order is journaled
(``prints_relined``); trashing and restoring a filed print ask ``F`` and are journaled.
"""

import pytest
from sqlalchemy import select

from backend.app.models.project import Project, ProjectEvent
from backend.tests.integration.test_order_issue_review_fixes import LEAVE, _fulfil, _lamp_order
from backend.tests.integration.test_orders_api import _completed_print, catalog  # noqa: F401
from backend.tests.integration.test_project_line_passthrough import (  # noqa: F401 - fixtures
    linked_file,
    order_line,
    printer_with_queue,
)
from backend.tests.integration.test_workshop_archive_rights import (  # noqa: F401 — the shared fixture
    _add,
    _archive,
    _order_of,
    _remove,
    desk,
)
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = pytest.mark.integration


async def _kinds(db, order_id: int) -> list[str]:
    rows = await db.execute(select(ProjectEvent.kind).where(ProjectEvent.project_id == order_id))
    return [kind for (kind,) in rows]


async def _close(db, order_id: int, status: str) -> None:
    order = await db.get(Project, order_id, populate_existing=True)
    order.status = status
    await db.commit()


class TestTheFilingClerk:
    @pytest.mark.asyncio
    async def test_the_filing_right_alone_files_and_unfiles_any_print(self, committing_client, db_session, desk):
        await _user(db_session, "fd_clerk", ["orders:read", "orders:file_prints"])
        ids = [desk["ownerless"], desk["theirs"]]
        assert (await _add(committing_client, desk["a"], ids, "fd_clerk")).status_code == 200
        assert (await _order_of(db_session, desk["theirs"]))[0] == desk["a"]
        assert (await _remove(committing_client, desk["a"], ids, "fd_clerk")).status_code == 200
        assert await _order_of(db_session, desk["theirs"]) == (None, None)

    @pytest.mark.asyncio
    async def test_a_reader_files_nothing(self, committing_client, db_session, desk):
        await _user(db_session, "fd_reader", ["orders:read", "archives:update_all"])
        r = await _add(committing_client, desk["a"], [desk["theirs"]], "fd_reader")
        assert r.status_code == 403
        assert await _order_of(db_session, desk["theirs"]) == (None, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["x-api-key", "bearer"])
    async def test_a_key_of_a_clerk_files_inside_its_workshop_scope(self, committing_client, db_session, desk, header):
        clerk = await _user(db_session, f"fd_clerk_key_{header}", ["orders:file_prints"])
        raw = await _key(db_session, clerk, can_manage_projects=True, can_manage_archives=False)
        headers = {"X-API-Key": raw} if header == "x-api-key" else {"Authorization": f"Bearer {raw}"}
        r = await committing_client.post(
            f"/api/v1/projects/{desk['a']}/add-archives", json={"archive_ids": [desk["ownerless"]]}, headers=headers
        )
        assert r.status_code == 200, r.text
        assert (await _order_of(db_session, desk["ownerless"]))[0] == desk["a"]


class TestANewLinkGoesToAnOpenOrder:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed", "cancelled"])
    async def test_a_closed_order_takes_no_print(self, committing_client, db_session, desk, status):
        await _close(db_session, desk["b"], status)
        r = await _add(committing_client, desk["b"], [desk["ownerless"]])
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "order_closed"
        assert await _order_of(db_session, desk["ownerless"]) == (None, None)

    @pytest.mark.asyncio
    async def test_a_print_still_leaves_a_closed_order(self, committing_client, db_session, desk):
        assert (await _add(committing_client, desk["b"], [desk["ownerless"]])).status_code == 200
        await _close(db_session, desk["b"], "cancelled")
        assert (await _remove(committing_client, desk["b"], [desk["ownerless"]])).status_code == 200
        assert await _order_of(db_session, desk["ownerless"]) == (None, None)


class TestALineChangeIsJournaled:
    @pytest.mark.asyncio
    async def test_moving_a_print_to_another_line_of_its_order_writes_the_journal(
        self, committing_client, db_session, desk
    ):
        first, second = desk["lines_a"]
        assert (await _add(committing_client, desk["a"], [desk["theirs"]], project_line_id=first)).status_code == 200
        before = await _kinds(db_session, desk["a"])
        moved = await _add(committing_client, desk["a"], [desk["theirs"]], project_line_id=second)
        assert moved.status_code == 200, moved.text
        assert await _order_of(db_session, desk["theirs"]) == (desk["a"], second)
        after = await _kinds(db_session, desk["a"])
        assert after.count("prints_relined") == before.count("prints_relined") + 1
        assert after.count("prints_filed") == before.count("prints_filed")

    @pytest.mark.asyncio
    async def test_the_editor_journals_a_line_change_too(self, committing_client, db_session, desk):
        first, second = desk["lines_a"]
        assert (await _add(committing_client, desk["a"], [desk["theirs"]], project_line_id=first)).status_code == 200
        r = await committing_client.patch(f"/api/v1/archives/{desk['theirs']}", json={"project_line_id": second})
        assert r.status_code == 200, r.text
        assert "prints_relined" in await _kinds(db_session, desk["a"])


class TestTheArchiveEditorsBinding:
    @pytest.mark.asyncio
    async def test_the_filing_right_beside_the_archives_own_moves_the_binding(
        self, committing_client, db_session, desk
    ):
        await _user(db_session, "fd_own_filer", ["archives:update_all", "orders:file_prints"])
        r = await committing_client.patch(
            f"/api/v1/archives/{desk['theirs']}", json={"project_id": desk["a"]}, headers=await _jwt("fd_own_filer")
        )
        assert r.status_code == 200, r.text
        assert (await _order_of(db_session, desk["theirs"]))[0] == desk["a"]

    @pytest.mark.asyncio
    async def test_a_binding_to_an_order_that_is_not_there_is_404(self, committing_client, db_session, desk):
        r = await committing_client.patch(f"/api/v1/archives/{desk['ownerless']}", json={"project_id": 999999})
        assert r.status_code == 404
        assert await _order_of(db_session, desk["ownerless"]) == (None, None)

    @pytest.mark.asyncio
    async def test_a_binding_to_a_closed_order_is_refused(self, committing_client, db_session, desk):
        await _close(db_session, desk["b"], "cancelled")
        r = await committing_client.patch(f"/api/v1/archives/{desk['ownerless']}", json={"project_id": desk["b"]})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "order_closed"
        assert await _order_of(db_session, desk["ownerless"]) == (None, None)

    @pytest.mark.asyncio
    async def test_a_line_change_inside_a_closed_order_is_refused(self, committing_client, db_session, desk):
        first, second = desk["lines_a"]
        assert (await _add(committing_client, desk["a"], [desk["theirs"]], project_line_id=first)).status_code == 200
        await _close(db_session, desk["a"], "completed")
        r = await committing_client.patch(f"/api/v1/archives/{desk['theirs']}", json={"project_line_id": second})
        assert r.status_code == 409
        assert await _order_of(db_session, desk["theirs"]) == (desk["a"], first)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["teleported", None])
    async def test_a_status_outside_the_closed_set_is_refused(self, committing_client, desk, status):
        r = await committing_client.patch(f"/api/v1/archives/{desk['ownerless']}", json={"status": status})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_a_received_print_does_not_leave_completed(self, committing_client, db_session, catalog):  # noqa: F811
        order, [line] = await _lamp_order(committing_client, catalog, [1])
        received = await _completed_print(db_session, order, catalog["file"].id)
        assert (await _fulfil(committing_client, order, [{"line_id": line, "receive": 1}])).status_code == 200
        r = await committing_client.patch(f"/api/v1/archives/{received.id}", json={"status": "failed"})
        assert (r.status_code, r.json()["detail"]) == (409, LEAVE)

    @pytest.mark.asyncio
    async def test_a_spare_print_may_leave_completed(self, committing_client, db_session, catalog):  # noqa: F811
        order, _ = await _lamp_order(committing_client, catalog, [1])
        spare = await _completed_print(db_session, order, catalog["file"].id)
        r = await committing_client.patch(f"/api/v1/archives/{spare.id}", json={"status": "failed"})
        assert r.status_code == 200, r.text


class TestTheTrash:
    @pytest.mark.asyncio
    async def test_trashing_a_filed_print_asks_the_filing_right_and_is_journaled(
        self, committing_client, db_session, desk
    ):
        assert (await _add(committing_client, desk["a"], [desk["theirs"]])).status_code == 200
        await _user(db_session, "fd_deleter", ["archives:delete_all"])
        await _user(db_session, "fd_deleter_filer", ["archives:delete_all", "orders:file_prints"])
        url = f"/api/v1/archives/{desk['theirs']}"
        assert (await committing_client.delete(url, headers=await _jwt("fd_deleter"))).status_code == 403
        assert (await committing_client.delete(url, headers=await _jwt("fd_deleter_filer"))).status_code == 200
        assert "print_trashed" in await _kinds(db_session, desk["a"])
        restore = f"/api/v1/archives/trash/{desk['theirs']}/restore"
        assert (await committing_client.post(restore, headers=await _jwt("fd_deleter"))).status_code == 403
        restored = await committing_client.post(restore, headers=await _jwt("fd_deleter_filer"))
        assert restored.status_code == 200, restored.text
        assert "print_restored" in await _kinds(db_session, desk["a"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("rights", "expected"),
        [
            (["orders:update", "archives:update_own", "archives:delete_own"], 200),
            (["archives:update_own", "archives:delete_own"], 403),
        ],
    )
    async def test_the_owner_trashes_an_own_filed_print_with_the_orders_right(
        self, committing_client, db_session, desk, rights, expected
    ):
        owner = await _user(db_session, f"fd_own_deleter_{expected}", rights)
        own = await _archive(db_session, owner=owner)
        assert (await _add(committing_client, desk["a"], [own.id])).status_code == 200
        r = await committing_client.delete(
            f"/api/v1/archives/{own.id}", headers=await _jwt(f"fd_own_deleter_{expected}")
        )
        assert r.status_code == expected, r.text

    @pytest.mark.asyncio
    async def test_an_unfiled_print_is_trashed_as_before(self, committing_client, db_session, desk):
        await _user(db_session, "fd_plain_deleter", ["archives:delete_all"])
        r = await committing_client.delete(
            f"/api/v1/archives/{desk['ownerless']}", headers=await _jwt("fd_plain_deleter")
        )
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_a_received_print_is_not_trashed(self, committing_client, db_session, catalog):  # noqa: F811
        order, [line] = await _lamp_order(committing_client, catalog, [1])
        received = await _completed_print(db_session, order, catalog["file"].id)
        assert (await _fulfil(committing_client, order, [{"line_id": line, "receive": 1}])).status_code == 200
        r = await committing_client.delete(f"/api/v1/archives/{received.id}")
        assert (r.status_code, r.json()["detail"]) == (409, LEAVE)


class TestCountingAnOldPrintIntoStock:
    @pytest.mark.asyncio
    async def test_it_needs_the_archive_to_be_visible(self, committing_client, db_session, desk):
        counter = await _user(db_session, "fd_counter", ["stock:adjust", "archives:read_own"])
        await _user(db_session, "fd_blind_counter", ["stock:adjust"])
        own = await _archive(db_session, owner=counter)
        url = "/api/v1/archives/{}/count-into-stock"
        assert (await committing_client.post(url.format(own.id), headers=await _jwt("fd_counter"))).status_code == 200
        assert (
            await committing_client.post(url.format(desk["theirs"]), headers=await _jwt("fd_counter"))
        ).status_code == 404
        blind = await committing_client.post(url.format(desk["theirs"]), headers=await _jwt("fd_blind_counter"))
        assert blind.status_code == 404


# ---------------------------------------------------------------------------
# Future work (O10, Q3): ``Fф`` = orders:file_prints OR orders:update, asked by every door
# that creates work under an order; the order must be open (``order_filing.resolve_link``).
# ---------------------------------------------------------------------------

FORBIDDEN = "filing_forbidden"


async def _queued_under(db, project_id: int) -> int:
    from sqlalchemy import func

    from backend.app.models.print_queue import PrintQueueItem

    return await db.scalar(select(func.count(PrintQueueItem.id)).where(PrintQueueItem.project_id == project_id))


async def _auto_queued_under(db, project_id: int) -> int:
    from sqlalchemy import func

    from backend.app.models.auto_queue import AutoQueueItem

    return await db.scalar(select(func.count(AutoQueueItem.id)).where(AutoQueueItem.project_id == project_id))


class TestTheQueueDoors:
    @pytest.mark.asyncio
    async def test_a_queue_row_under_an_order_asks_the_filing_right(
        self, committing_client, db_session, order_line, printer_with_queue, linked_file
    ):
        project, _ = order_line
        await _user(db_session, "ff_queuer", ["queue:create", "library:read_all"])
        await _user(db_session, "ff_queuer_clerk", ["queue:create", "library:read_all", "orders:file_prints"])
        body = {"queue_id": printer_with_queue.queue_id, "library_file_id": linked_file.id}
        refused = await committing_client.post(
            "/api/v1/queue/", json={**body, "project_id": project.id}, headers=await _jwt("ff_queuer")
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["error"] == FORBIDDEN
        assert await _queued_under(db_session, project.id) == 0
        plain = await committing_client.post("/api/v1/queue/", json=body, headers=await _jwt("ff_queuer"))
        assert plain.status_code == 200, plain.text
        filed = await committing_client.post(
            "/api/v1/queue/", json={**body, "project_id": project.id}, headers=await _jwt("ff_queuer_clerk")
        )
        assert filed.status_code == 200, filed.text
        assert await _queued_under(db_session, project.id) == 1

    @pytest.mark.asyncio
    async def test_a_closed_order_takes_no_queue_row(
        self, committing_client, db_session, order_line, printer_with_queue, linked_file
    ):
        project, line = order_line
        await _close(db_session, project.id, "completed")
        body = {"queue_id": printer_with_queue.queue_id, "library_file_id": linked_file.id}
        for extra in ({"project_id": project.id}, {"project_line_id": line.id}):
            r = await committing_client.post("/api/v1/queue/", json={**body, **extra})
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "order_closed"
        assert await _queued_under(db_session, project.id) == 0

    @pytest.mark.asyncio
    async def test_the_next_block_asks_the_filing_right(
        self, committing_client, db_session, order_line, printer_with_queue, linked_file
    ):
        project, _ = order_line
        await _user(db_session, "ff_next", ["queue:create", "queue:reorder", "library:read_all"])
        item = {
            "queue_id": printer_with_queue.queue_id,
            "library_file_id": linked_file.id,
            "enqueue_position": "next",
            "project_id": project.id,
        }
        r = await committing_client.post(
            "/api/v1/queue/next-block", json={"items": [item]}, headers=await _jwt("ff_next")
        )
        assert r.status_code == 403, r.text
        assert await _queued_under(db_session, project.id) == 0


class TestTheAutoQueueDoor:
    @pytest.mark.asyncio
    async def test_an_auto_queue_row_under_an_order_asks_the_filing_right(
        self, committing_client, db_session, order_line, linked_file
    ):
        project, _ = order_line
        await _user(db_session, "ff_auto", ["queue:create", "library:read_all"])
        await _user(db_session, "ff_auto_desk", ["queue:create", "library:read_all", "orders:update"])
        body = {"library_file_id": linked_file.id, "project_id": project.id}
        refused = await committing_client.post("/api/v1/auto-queue/", json=body, headers=await _jwt("ff_auto"))
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["error"] == FORBIDDEN
        filed = await committing_client.post("/api/v1/auto-queue/", json=body, headers=await _jwt("ff_auto_desk"))
        assert filed.status_code in (200, 201), filed.text
        assert await _auto_queued_under(db_session, project.id) == 1

    @pytest.mark.asyncio
    async def test_a_closed_order_takes_no_auto_queue_row(self, committing_client, db_session, order_line, linked_file):
        project, _ = order_line
        await _close(db_session, project.id, "cancelled")
        r = await committing_client.post(
            "/api/v1/auto-queue/", json={"library_file_id": linked_file.id, "project_id": project.id}
        )
        assert r.status_code == 409, r.text

    @pytest.mark.asyncio
    async def test_the_source_must_be_visible_as_in_the_printer_queue(self, committing_client, db_session, linked_file):
        await _user(db_session, "ff_auto_own", ["queue:create", "library:read_own"])
        r = await committing_client.post(
            "/api/v1/auto-queue/", json={"library_file_id": linked_file.id}, headers=await _jwt("ff_auto_own")
        )
        assert r.status_code == 404, r.text


class TestTheLibraryPrintDoor:
    @pytest.mark.asyncio
    async def test_a_direct_print_under_an_order_asks_the_filing_right_and_a_visible_file(
        self, committing_client, db_session, order_line, printer_with_queue, linked_file
    ):
        from unittest.mock import AsyncMock, patch

        project, line = order_line
        await _user(db_session, "ff_printer", ["printers:control", "library:read_all"])
        await _user(db_session, "ff_printer_desk", ["printers:control", "library:read_all", "orders:update"])
        await _user(db_session, "ff_printer_blind", ["printers:control", "orders:update"])
        url = f"/api/v1/library/files/{linked_file.id}/print?printer_id={printer_with_queue.id}"
        with (
            patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
            patch(
                "backend.app.services.background_dispatch.background_dispatch.dispatch_print_library_file",
                new=AsyncMock(return_value={"status": "dispatched", "dispatch_job_id": 1, "dispatch_position": 1}),
            ) as dispatch,
        ):
            refused = await committing_client.post(
                url, json={"project_id": project.id}, headers=await _jwt("ff_printer")
            )
            assert refused.status_code == 403, refused.text
            assert refused.json()["detail"]["error"] == FORBIDDEN
            blind = await committing_client.post(url, json={}, headers=await _jwt("ff_printer_blind"))
            assert blind.status_code == 404, blind.text
            assert dispatch.await_count == 0
            filed = await committing_client.post(
                url, json={"project_line_id": line.id}, headers=await _jwt("ff_printer_desk")
            )
            assert filed.status_code == 200, filed.text
            assert dispatch.await_args.kwargs["project_id"] == project.id
            await _close(db_session, project.id, "completed")
            closed = await committing_client.post(url, json={"project_id": project.id})
            assert closed.status_code == 409, closed.text


class TestTheOrderPlanDoors:
    @pytest.mark.asyncio
    async def test_the_plan_lets_the_filing_clerk_in_and_refuses_a_closed_order(
        self, committing_client, db_session, order_line
    ):
        project, line = order_line
        await _user(db_session, "ff_planner", ["queue:create"])
        await _user(db_session, "ff_planner_clerk", ["queue:create", "orders:file_prints"])
        body = {"items": [{"plate_id": 1, "count": 1, "line_id": line.id}], "target": {"kind": "auto"}}
        url = f"/api/v1/projects/{project.id}/plan/enqueue"
        assert (await committing_client.post(url, json=body, headers=await _jwt("ff_planner"))).status_code == 403
        await _close(db_session, project.id, "completed")
        r = await committing_client.post(url, json=body, headers=await _jwt("ff_planner_clerk"))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "order_closed"

    @pytest.mark.asyncio
    async def test_a_line_rebalance_asks_the_filing_right(self, committing_client, db_session, order_line):
        project, line = order_line
        await _user(db_session, "ff_balancer", ["queue:update_all"])
        await _user(db_session, "ff_balancer_clerk", ["queue:update_all", "orders:file_prints"])
        url = f"/api/v1/projects/{project.id}/lines/{line.id}/rebalance"
        assert (await committing_client.post(url, headers=await _jwt("ff_balancer"))).status_code == 403
        r = await committing_client.post(url, headers=await _jwt("ff_balancer_clerk"))
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_the_auto_queue_rebalance_asks_the_filing_right(self, committing_client, db_session):
        await _user(db_session, "ff_router", ["queue:update_all"])
        await _user(db_session, "ff_router_desk", ["queue:update_all", "orders:update"])
        body = {"item_ids": [999999]}
        refused = await committing_client.post(
            "/api/v1/auto-queue/rebalance", json=body, headers=await _jwt("ff_router")
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["error"] == FORBIDDEN
        r = await committing_client.post(
            "/api/v1/auto-queue/rebalance", json=body, headers=await _jwt("ff_router_desk")
        )
        assert r.status_code == 200, r.text


class TestRefilingQueueRows:
    @pytest.fixture
    async def rows(self, db_session, order_line, printer_with_queue, linked_file):
        from backend.app.models.print_queue import PrintQueueItem

        project, _ = order_line
        other = Project(name="Old order")
        db_session.add(other)
        await db_session.flush()
        made = {}
        for status in ("pending", "completed"):
            row = PrintQueueItem(
                queue_id=printer_with_queue.queue_id,
                library_file_id=linked_file.id,
                project_id=other.id,
                status=status,
            )
            db_session.add(row)
            await db_session.flush()
            made[status] = row.id
        await db_session.commit()
        return {"order": project.id, "old": other.id, **made}

    @pytest.mark.asyncio
    async def test_it_asks_the_queue_right_too_and_journals_the_old_order(self, committing_client, db_session, rows):
        await _user(db_session, "ff_refiler", ["orders:update"])
        await _user(db_session, "ff_refiler_queue", ["orders:update", "queue:update_all"])
        url = f"/api/v1/projects/{rows['order']}/add-queue"
        body = {"queue_item_ids": [rows["pending"]]}
        assert (await committing_client.post(url, json=body, headers=await _jwt("ff_refiler"))).status_code == 403
        r = await committing_client.post(url, json=body, headers=await _jwt("ff_refiler_queue"))
        assert r.status_code == 200, r.text
        assert "queue_items_filed" in await _kinds(db_session, rows["order"])
        assert "queue_items_unfiled" in await _kinds(db_session, rows["old"])

    @pytest.mark.asyncio
    async def test_only_pending_work_is_refiled(self, committing_client, db_session, rows):
        r = await committing_client.post(
            f"/api/v1/projects/{rows['order']}/add-queue",
            json={"queue_item_ids": [rows["pending"], rows["completed"]]},
        )
        assert r.status_code == 409, r.text
        assert await _queued_under(db_session, rows["order"]) == 0


# ---------------------------------------------------------------------------
# Inherited links (O10): a reprint and a clone inherit the source's order by default and then
# ask ``Fф`` and an open order; ``keep_order=false`` is the explicit choice to print without it.
# ---------------------------------------------------------------------------


@pytest.fixture
async def filed_print(db_session, order_line, linked_file):
    """A completed print of an order's line, its 3MF on disk."""
    from backend.app.models.archive import PrintArchive

    project, line = order_line
    archive = PrintArchive(
        filename=linked_file.filename,
        file_path=linked_file.file_path,
        file_size=linked_file.file_size,
        status="completed",
        project_id=project.id,
        project_line_id=line.id,
    )
    db_session.add(archive)
    await db_session.commit()
    return {"archive": archive.id, "order": project.id, "line": line.id}


class TestTheReprintDoor:
    @pytest.fixture
    def dispatch(self):
        from unittest.mock import AsyncMock, patch

        with (
            patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
            patch(
                "backend.app.services.background_dispatch.background_dispatch.dispatch_reprint_archive",
                new=AsyncMock(return_value={"status": "dispatched", "dispatch_job_id": 1, "dispatch_position": 1}),
            ) as dispatched,
        ):
            yield dispatched

    @pytest.mark.asyncio
    async def test_an_inherited_order_asks_the_filing_right(
        self, committing_client, db_session, filed_print, printer_with_queue, dispatch
    ):
        await _user(db_session, "ff_reprinter", ["archives:reprint_all"])
        await _user(db_session, "ff_reprinter_desk", ["archives:reprint_all", "orders:update"])
        url = f"/api/v1/archives/{filed_print['archive']}/reprint?printer_id={printer_with_queue.id}"
        refused = await committing_client.post(url, json={}, headers=await _jwt("ff_reprinter"))
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["error"] == FORBIDDEN
        assert dispatch.await_count == 0
        filed = await committing_client.post(url, json={}, headers=await _jwt("ff_reprinter_desk"))
        assert filed.status_code == 200, filed.text
        assert dispatch.await_args.kwargs["project_id"] == filed_print["order"]
        assert dispatch.await_args.kwargs["project_line_id"] == filed_print["line"]

    @pytest.mark.asyncio
    async def test_printing_without_the_order_is_an_explicit_choice(
        self, committing_client, db_session, filed_print, printer_with_queue, dispatch
    ):
        await _user(db_session, "ff_reprinter_plain", ["archives:reprint_all"])
        url = f"/api/v1/archives/{filed_print['archive']}/reprint?printer_id={printer_with_queue.id}"
        r = await committing_client.post(url, json={"keep_order": False}, headers=await _jwt("ff_reprinter_plain"))
        assert r.status_code == 200, r.text
        assert dispatch.await_args.kwargs["project_id"] is None
        assert dispatch.await_args.kwargs["project_line_id"] is None
        assert "keep_order" not in dispatch.await_args.kwargs["options"]

    @pytest.mark.asyncio
    async def test_a_closed_order_is_not_inherited(
        self, committing_client, db_session, filed_print, printer_with_queue, dispatch
    ):
        await _close(db_session, filed_print["order"], "completed")
        url = f"/api/v1/archives/{filed_print['archive']}/reprint?printer_id={printer_with_queue.id}"
        r = await committing_client.post(url, json={})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "order_closed"
        assert (await committing_client.post(url, json={"keep_order": False})).status_code == 200

    @pytest.mark.asyncio
    async def test_copies_carry_the_choice_too(self, committing_client, db_session, filed_print, printer_with_queue):
        from unittest.mock import AsyncMock, patch

        url = f"/api/v1/archives/{filed_print['archive']}/reprint?printer_id={printer_with_queue.id}"
        with (
            patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
            patch(
                "backend.app.services.queue_batch.enqueue_batch_copies", new=AsyncMock(return_value=([], "b"))
            ) as copies,
        ):
            r = await committing_client.post(url, json={"quantity": 2, "keep_order": False})
        assert r.status_code == 200, r.text
        assert copies.await_args.kwargs["project_id"] is None
        assert copies.await_args.kwargs["project_line_id"] is None


class TestTheCloneDoors:
    @pytest.fixture
    async def source(self, committing_client, db_session, order_line, printer_with_queue, linked_file):
        """A pending queue row under the order, created by somebody else."""
        from backend.app.models.print_queue import PrintQueueItem

        project, line = order_line
        r = await committing_client.post(
            "/api/v1/queue/",
            json={
                "queue_id": printer_with_queue.queue_id,
                "library_file_id": linked_file.id,
                "project_line_id": line.id,
                "quantity": 1,
            },
        )
        assert r.status_code == 200, r.text
        other = await _user(db_session, "ff_clone_owner", [])
        row = await db_session.get(PrintQueueItem, r.json()["id"])
        row.created_by_id = other.id
        await db_session.commit()
        return {"id": row.id, "order": project.id, "line": line.id}

    async def _clone(self, client, item_id: int, who: str | None, **params):
        headers = await _jwt(who) if who else None
        return await client.post(f"/api/v1/queue/{item_id}/clone", params=params, headers=headers)

    @pytest.mark.asyncio
    async def test_someone_elses_row_is_cloned_only_with_the_queues_read_all(
        self, committing_client, db_session, source
    ):
        await _user(db_session, "ff_cloner_blind", ["queue:create", "orders:update"])
        r = await self._clone(committing_client, source["id"], "ff_cloner_blind")
        assert r.status_code == 404, r.text
        assert await _queued_under(db_session, source["order"]) == 1

    @pytest.mark.asyncio
    async def test_an_inherited_order_asks_the_filing_right_and_the_cloner_owns_the_copy(
        self, committing_client, db_session, source
    ):
        from backend.app.models.print_queue import PrintQueueItem

        await _user(db_session, "ff_cloner", ["queue:create", "queue:read_all"])
        desk = await _user(db_session, "ff_cloner_desk", ["queue:create", "queue:read_all", "orders:update"])
        refused = await self._clone(committing_client, source["id"], "ff_cloner")
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["error"] == FORBIDDEN
        r = await self._clone(committing_client, source["id"], "ff_cloner_desk")
        assert r.status_code == 200, r.text
        copy = await db_session.get(PrintQueueItem, r.json()["id"], populate_existing=True)
        assert (copy.project_id, copy.project_line_id) == (source["order"], source["line"])
        assert copy.created_by_id == desk.id

    @pytest.mark.asyncio
    async def test_a_clone_without_the_order_is_an_explicit_choice(self, committing_client, db_session, source):
        from backend.app.models.print_queue import PrintQueueItem

        await _user(db_session, "ff_cloner_plain", ["queue:create", "queue:read_all"])
        r = await self._clone(committing_client, source["id"], "ff_cloner_plain", keep_order="false")
        assert r.status_code == 200, r.text
        copy = await db_session.get(PrintQueueItem, r.json()["id"], populate_existing=True)
        assert (copy.project_id, copy.project_line_id) == (None, None)

    @pytest.mark.asyncio
    async def test_a_closed_order_is_not_inherited_by_a_clone(self, committing_client, db_session, source):
        await _close(db_session, source["order"], "cancelled")
        r = await self._clone(committing_client, source["id"], None)
        assert r.status_code == 409, r.text
        assert await _queued_under(db_session, source["order"]) == 1
