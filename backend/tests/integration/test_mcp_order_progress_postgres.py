"""Single-attribution regression and bounded PostgreSQL measurement; isolated fixtures only."""

import json
from time import perf_counter

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.tests.integration.test_embedded_postgres_live import live_settings
from backend.tests.integration.test_g01_a_postgres import test_engine
from backend.tests.integration.test_mcp_orders import progress


@pytest.mark.asyncio
async def test_order_progress_attributes_once_per_page_on_postgres(test_engine, monkeypatch):
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))
    from unittest.mock import Mock

    from backend.app.services import order_fulfilment, order_metrics

    attr = Mock(wraps=order_metrics.attribute)
    monkeypatch.setattr(order_metrics, "attribute", attr)
    monkeypatch.setattr(order_fulfilment, "attribute", attr)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as db:
        order, product = Project(name="Reader load"), Product(name="Part product")
        db.add_all([order, product])
        await db.flush()
        db.add(ProductPart(product_id=product.id, kind="printed", name="Part", name_key="part", qty_per_unit=1))
        lines = [ProjectLine(project_id=order.id, product_id=product.id, quantity=1000) for _ in range(16)]
        db.add_all(lines)
        await db.commit()
        order_id = order.id
        statements = []
        event.listen(
            test_engine.sync_engine,
            "before_cursor_execute",
            lambda conn, cursor, statement, params, context, many: statements.append(statement[:10]),
        )
        previous = 0
        for size in (25, 2500, 9500):
            values = [
                {
                    "project_id": order_id,
                    "project_line_id": lines[i % 16].id,
                    "filename": "fixture",
                    "file_path": "",
                    "file_size": 0,
                    "status": "completed",
                    "quantity": 1,
                }
                for i in range(previous, size)
            ]
            ids = (await db.scalars(insert(PrintArchive).returning(PrintArchive.id), values)).all()
            await db.execute(
                insert(PrintArchivePart),
                [{"archive_id": i, "name": "Part", "name_key": "part", "quantity": 1, "defective": 0} for i in ids],
            )
            await db.commit()
            cursor = None
            for page in (1, 2):
                async with sessions() as reader:
                    attr.reset_mock()
                    statements.clear()
                    started = perf_counter()
                    result = await progress(reader, {"order": order_id}, limit=1, cursor=cursor)
                    elapsed = perf_counter() - started
                    assert len(result.data["lines"]) == 1 and result.next_cursor
                    assert attr.call_count == 1
                    cursor = result.next_cursor
                    print(
                        "READER_MEASURE "
                        + json.dumps(
                            {
                                "archives": size,
                                "parts": size,
                                "total_lines": 16,
                                "page": page,
                                "returned": 1,
                                "seconds": round(elapsed, 3),
                                "statements": len(statements),
                                "attributions": attr.call_count,
                            }
                        ),
                        flush=True,
                    )
            previous = size
