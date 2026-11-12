"""A link that was allowed is data from then on (WS-13 E13 O11, Q-08).

The right to file work under an order is asked when the link is made. Revoking the right,
deactivating the person who made it or deleting them does not unlink the work: a print under
way would otherwise finish without being counted. Deleting a user forgets them as the creator
in BOTH queues — on SQLite nothing else does, and a later user given the same id would inherit
somebody else's rows.
"""

import pytest
from sqlalchemy import update

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.group import Group
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.user import User
from backend.tests.integration.test_project_line_passthrough import (  # noqa: F401 — fixtures
    linked_file,
    order_line,
    printer_with_queue,
)
from backend.tests.integration.test_workshop_library_rights import _jwt, _user

pytestmark = pytest.mark.integration


@pytest.fixture
async def queued(committing_client, db_session, order_line, printer_with_queue, linked_file):
    """One row in each queue under the order, made by ``fin_planner`` with the right to file."""
    project, line = order_line
    planner = await _user(db_session, "fin_planner", ["queue:create", "library:read_all", "orders:update"])
    auto = await committing_client.post(
        "/api/v1/auto-queue/",
        json={"library_file_id": linked_file.id, "project_line_id": line.id},
        headers=await _jwt("fin_planner"),
    )
    assert auto.status_code in (200, 201), auto.text
    printer_row = await committing_client.post(
        "/api/v1/queue/",
        json={"queue_id": printer_with_queue.queue_id, "library_file_id": linked_file.id, "project_line_id": line.id},
        headers=await _jwt("fin_planner"),
    )
    assert printer_row.status_code == 200, printer_row.text
    return {
        "planner": planner.id,
        "auto": auto.json()["id"],
        "row": printer_row.json()["id"],
        "order": project.id,
        "line": line.id,
    }


async def _links(db, queued) -> tuple:
    auto = await db.get(AutoQueueItem, queued["auto"], populate_existing=True)
    row = await db.get(PrintQueueItem, queued["row"], populate_existing=True)
    return (auto.project_id, auto.project_line_id), (row.project_id, row.project_line_id)


@pytest.mark.asyncio
async def test_revoking_the_right_or_deactivating_the_creator_unlinks_nothing(db_session, queued):
    linked = ((queued["order"], queued["line"]),) * 2
    await db_session.execute(update(Group).where(Group.name == "grp-fin_planner").values(permissions=[]))
    await db_session.execute(update(User).where(User.id == queued["planner"]).values(is_active=False))
    await db_session.commit()
    assert await _links(db_session, queued) == linked


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_items", [False, True])
async def test_deleting_the_creator_forgets_them_in_the_auto_queue_and_keeps_the_link(
    committing_client, db_session, queued, delete_items
):
    r = await committing_client.delete(
        f"/api/v1/users/{queued['planner']}", params={"delete_items": str(delete_items).lower()}
    )
    assert r.status_code == 204, r.text
    auto = await db_session.get(AutoQueueItem, queued["auto"], populate_existing=True)
    assert auto.created_by_id is None
    assert (auto.project_id, auto.project_line_id) == (queued["order"], queued["line"])


# The carriers of work already allowed — the scheduler, the auto-queue's distributor, the
# rebalance tick, the dispatcher and the batch writer they share — never ask anyone's rights:
# the link was judged when it was made (O11). A right read here would stop a print under way.
CARRIERS = (
    "print_scheduler.py",
    "auto_queue_eligibility.py",
    "auto_queue_ams.py",
    "queue_rebalance.py",
    "background_dispatch.py",
    "queue_batch.py",
)
RIGHTS_WORDS = ("has_permission(", "Permission.", "RequestCredentials", "creds.check", "creds.allows")


def test_the_carriers_of_allowed_work_read_no_rights():
    from pathlib import Path

    services = Path(__file__).resolve().parents[2] / "app" / "services"
    readers = {
        name: [word for word in RIGHTS_WORDS if word in (services / name).read_text(encoding="utf-8")]
        for name in CARRIERS
        if (services / name).exists()
    }
    assert len(readers) == len(CARRIERS), "a carrier moved — point this test at its new home"
    assert {name: words for name, words in readers.items() if words} == {}
