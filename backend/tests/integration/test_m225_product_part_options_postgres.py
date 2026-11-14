"""m225 on a disposable PostgreSQL: the binding moves into product_part_options, the column and its index go."""

import pytest
from sqlalchemy import inspect, text

from backend.app.migrations import helpers, m225_product_part_options as m225
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


async def test_an_existing_postgres_database_moves_its_bindings(test_engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)
    async with test_engine.begin() as conn:  # the database as it stood before m225
        await conn.execute(text("DROP TABLE product_part_options"))
        await conn.execute(
            text(
                "ALTER TABLE product_parts ADD COLUMN variant_option_id INTEGER"
                " REFERENCES product_variant_options(id) ON DELETE SET NULL"
            )
        )
        await conn.execute(text("CREATE INDEX ix_product_parts_variant_option_id ON product_parts (variant_option_id)"))
        await conn.execute(text("INSERT INTO products (id, name) VALUES (1, 'Lamp')"))
        await conn.execute(
            text("INSERT INTO product_variant_groups (id, product_id, name) VALUES (10, 1, 'Tail'), (11, 1, 'Shade')")
        )
        await conn.execute(
            text(
                "INSERT INTO product_variant_options (id, group_id, name) VALUES (20, 10, 'Straight'), (21, 11, 'Cone')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO product_parts (id, product_id, kind, name, name_key, variant_option_id) VALUES"
                " (100, 1, 'printed', 'Cone', 'cone', 21), (101, 1, 'printed', 'Base', 'base', NULL)"
            )
        )

    for _ in range(2):  # DEBUG re-runs the newest migration
        async with test_engine.begin() as conn:
            await m225.upgrade(conn)

    async with test_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT part_id, option_id, group_id FROM product_part_options"))).all()
        assert [tuple(r) for r in rows] == [(100, 21, 11)]
        assert not await helpers.column_exists(conn, "product_parts", "variant_option_id")
        parts = (await conn.execute(text("SELECT id, name_key FROM product_parts ORDER BY id"))).all()
        assert [tuple(r) for r in parts] == [(100, "cone"), (101, "base")]

        def _schema(sync):
            insp = inspect(sync)
            return (
                {i["name"] for i in insp.get_indexes("product_parts")},
                {u["name"] for u in insp.get_unique_constraints("product_parts")},
                insp.get_pk_constraint("product_part_options")["constrained_columns"],
                {
                    (f["referred_table"], tuple(f["constrained_columns"]), f["options"].get("ondelete"))
                    for f in insp.get_foreign_keys("product_part_options")
                },
                {i["name"]: i["column_names"] for i in insp.get_indexes("product_part_options")},
            )

        part_indexes, part_uniques, pk, fks, option_indexes = await conn.run_sync(_schema)
        assert "ix_product_parts_variant_option_id" not in part_indexes
        assert "uq_product_parts_key" in part_uniques
        assert pk == ["part_id", "option_id"]
        assert fks == {
            ("product_parts", ("part_id",), "CASCADE"),
            ("product_variant_options", ("option_id",), "CASCADE"),
            ("product_variant_groups", ("group_id",), "CASCADE"),
        }
        assert option_indexes == {"ix_product_part_options_group_id": ["group_id"]}
