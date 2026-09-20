"""Defects from the order page: the print's parts under the order's own permission."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.project import Project
from backend.tests.integration.test_queue_rebalance_api import _as_viewer

pytestmark = pytest.mark.integration


async def _order_print(db_session, project_id: int, parts: dict[str, int] | None) -> PrintArchive:
    archive = PrintArchive(
        project_id=project_id,
        filename="a.3mf",
        print_name="A",
        file_path="x/a.3mf",
        file_size=1,
        status="completed",
        quantity=sum(parts.values()) if parts else 3,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.flush()
    for name, qty in (parts or {}).items():
        db_session.add(
            PrintArchivePart(
                archive_id=archive.id, name=name, name_key=name.lower(), identify_ids=list(range(qty)), quantity=qty
            )
        )
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def _order(db_session) -> Project:
    project = Project(name="O", status="active")
    db_session.add(project)
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_parts_are_read_and_written_under_the_order(committing_client, db_session):
    order = await _order(db_session)
    archive = await _order_print(db_session, order.id, {"lid": 2, "base": 4})
    rows = {
        r.name_key: r
        for r in (
            await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id))
        ).scalars()
    }

    got = await committing_client.get(f"/api/v1/projects/{order.id}/archives/{archive.id}/parts")
    assert got.status_code == 200, got.text
    assert {p["name_key"]: p["quantity"] for p in got.json()["parts"]} == {"lid": 2, "base": 4}
    assert got.json()["defective_count"] == 0 and got.json()["quantity"] == 6

    wrote = await committing_client.post(
        f"/api/v1/projects/{order.id}/archives/{archive.id}/defects",
        json={"parts": [{"id": rows["lid"].id, "defective": 9}, {"id": rows["base"].id, "defective": 1}]},
    )
    assert wrote.status_code == 200, wrote.text
    body = wrote.json()
    assert body["defective_count"] == 3
    # ⚠️ No ``ledger_refused_parts`` on this route's shape, by design: a print it
    # can reach is FILED under an order, and the shelf correction is a no-op for
    # those — the field was structurally always 0. The refusal is reported on the
    # plate answers and in Telegram, where an order-less print is graded.
    assert "ledger_refused_parts" not in body
    assert {p["name_key"]: p["defective"] for p in body["parts"]} == {"lid": 2, "base": 1}
    # Read the id BEFORE expiring: an expired attribute reloads itself, and a
    # lazy load from plain async code is a MissingGreenlet, not a query.
    archive_id = archive.id
    db_session.expire_all()
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 3


@pytest.mark.asyncio
async def test_a_flat_count_when_the_print_has_no_part_rows(committing_client, db_session):
    order = await _order(db_session)
    archive = await _order_print(db_session, order.id, None)
    wrote = await committing_client.post(
        f"/api/v1/projects/{order.id}/archives/{archive.id}/defects", json={"defective_count": 7}
    )
    assert wrote.status_code == 200, wrote.text
    assert wrote.json()["defective_count"] == 3, "clamped to the print's quantity"


@pytest.mark.asyncio
async def test_a_print_of_another_order_is_not_found_here(committing_client, db_session):
    order, other = await _order(db_session), await _order(db_session)
    archive = await _order_print(db_session, other.id, {"lid": 2})
    for method, url, body in (
        ("get", f"/api/v1/projects/{order.id}/archives/{archive.id}/parts", None),
        ("post", f"/api/v1/projects/{order.id}/archives/{archive.id}/defects", {"defective_count": 1}),
    ):
        resp = await getattr(committing_client, method)(url, json=body) if body else await committing_client.get(url)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Print not found in this order"


@pytest.mark.asyncio
async def test_writing_needs_projects_update(committing_client, db_session):
    order = await _order(db_session)
    archive = await _order_print(db_session, order.id, {"lid": 2})
    await _as_viewer(committing_client, db_session)
    assert (await committing_client.get(f"/api/v1/projects/{order.id}/archives/{archive.id}/parts")).status_code == 200
    resp = await committing_client.post(
        f"/api/v1/projects/{order.id}/archives/{archive.id}/defects", json={"defective_count": 1}
    )
    assert resp.status_code == 403
