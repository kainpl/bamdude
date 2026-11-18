"""C02/C12: account deletion is an order exit, including C1 and named audit."""

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.project import Project, ProjectEvent
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.tests.integration.test_order_issue_review_fixes import _fulfil, _lamp_order
from backend.tests.integration.test_orders_api import _completed_print, catalog  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_unfiling_plans_foreign_library_provenance_products_before_order_and_print_locks(
    committing_client, db_session, catalog, monkeypatch
):
    from backend.app.core.lock_ledger import ledger
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.services import part_stock

    other = Product(name="Another product sharing the source")
    db_session.add(other)
    await db_session.flush()
    part = ProductPart(
        product_id=other.id, kind="printed", name="shade", name_key="shade", aliases=["shade"], qty_per_unit=1
    )
    db_session.add_all([part, ProductPlate(product_id=other.id, library_file_id=catalog["file"].id, plate_index=0)])
    await db_session.commit()
    order_id, _lines = await _lamp_order(committing_client, catalog, [1])
    archive = await _completed_print(db_session, order_id, catalog["file"].id)
    owner = User(username="foreign-source-print-owner")
    db_session.add(owner)
    await db_session.flush()
    archive.created_by_id = owner.id
    await db_session.commit()
    user_id, source_id, part_id, other_id = owner.id, catalog["file"].id, part.id, other.id
    original = part_stock.credit_unfiled_print
    called = []

    async def guarded(db, row, **kwargs):
        held = ledger(db)
        assert {catalog["product"].id, other_id} <= held.gates
        assert ("product_parts", part_id) in held.locked_rows
        called.append(row.id)
        return await original(db, row, **kwargs)

    monkeypatch.setattr(part_stock, "credit_unfiled_print", guarded)
    response = await committing_client.delete(f"/api/v1/users/{user_id}?delete_items=true")
    assert response.status_code == 200, response.text
    assert called == [archive.id]
    assert await db_session.get(LibraryFile, source_id, populate_existing=True) is not None
    assert await db_session.get(Product, other_id, populate_existing=True) is not None


@pytest.mark.parametrize("received", [False, True])
async def test_user_deletion_rechecks_the_whole_order_and_journals_print_name(
    committing_client, db_session, catalog, received
):
    order_id, [line_id] = await _lamp_order(committing_client, catalog, [1])
    archive = await _completed_print(db_session, order_id, catalog["file"].id)
    owner = User(username="order-content-owner")
    db_session.add(owner)
    await db_session.flush()
    archive.created_by_id = owner.id
    archive.print_name = "A named print that will disappear"
    await db_session.commit()
    user_id, archive_id = owner.id, archive.id
    if received:
        response = await _fulfil(committing_client, order_id, [{"line_id": line_id, "receive": 1}])
        assert response.status_code == 200, response.text
    response = await committing_client.delete(f"/api/v1/users/{user_id}?delete_items=true")
    assert response.status_code == (409 if received else 200), response.text
    db_session.expire_all()
    assert await db_session.get(Project, order_id) is not None
    if received:
        assert await db_session.get(User, user_id) is not None
        assert (await db_session.get(PrintArchive, archive_id)).project_id == order_id
        assert (await db_session.get(ProjectLine, line_id)).received == 1
    else:
        assert response.json()["affected_orders"] == [{"id": order_id, "name": "Lamps", "prints": 1, "jobs": 0}]
        assert await db_session.get(User, user_id) is None
        assert await db_session.get(PrintArchive, archive_id) is None
        events = list(
            await db_session.scalars(
                select(ProjectEvent).where(ProjectEvent.project_id == order_id, ProjectEvent.kind == "prints_unfiled")
            )
        )
        assert len(events) == 1
        assert events[0].payload["prints"] == [{"archive_id": archive_id, "name": "A named print that will disappear"}]
