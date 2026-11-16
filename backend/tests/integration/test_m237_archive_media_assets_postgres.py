"""m237 on a disposable PostgreSQL: the same table a fresh install has, its two rules enforced, the seed (G10 C, M14)."""

from datetime import datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations import helpers, m237_archive_media_assets as m237
from backend.app.models.archive import PrintArchive
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]

INSERT = text(
    "INSERT INTO archive_media_assets (asset_uuid, archive_id, archive_identity, role, revision, relative_path, "
    "content_sha256, origin, remote_printer_identity, remote_storage, remote_path, created_at) "
    "VALUES (:u, :a, :i, :role, :rev, :p, :h, :o, :rp, :rs, :rpath, :t)"
)


def _row(uuid, archive, role="primary", rev=1, sha=None, origin="legacy", claim=(None, None, None)):
    return {
        "u": uuid,
        "a": archive,
        "i": f"ID-{archive}",
        "role": role,
        "rev": rev,
        "p": f"{uuid}.mp4",
        "h": sha,
        "o": origin,
        "rp": claim[0],
        "rs": claim[1],
        "rpath": claim[2],
        "t": datetime(2026, 1, 1),
    }


def _schema(sync):
    insp = inspect(sync)
    table = "archive_media_assets"
    return (
        [(c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(table)],
        {
            i["name"]: (i["unique"], i["column_names"], str(i.get("dialect_options", {}).get("postgresql_where") or ""))
            for i in insp.get_indexes(table)
        },
        {u["name"]: u["column_names"] for u in insp.get_unique_constraints(table)},
        {
            (f["referred_table"], tuple(f["constrained_columns"]), f["options"].get("ondelete"))
            for f in insp.get_foreign_keys(table)
        },
        insp.get_pk_constraint(table)["constrained_columns"],
    )


async def _archives(engine, count):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        rows = [
            PrintArchive(filename="a.3mf", file_path="", file_size=1, status="completed", recovery_identity=f"ID-{n}")
            for n in range(1, count + 1)
        ]
        db.add_all(rows)
        await db.commit()
        return [r.id for r in rows]


async def test_the_upgrade_builds_the_table_a_fresh_install_has(test_engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)
    async with test_engine.connect() as conn:
        fresh = await conn.run_sync(_schema)  # ``create_all`` from the model

    async with test_engine.begin() as conn:  # the database as it stood before m237
        await conn.execute(text("DROP TABLE archive_media_assets"))
    for _ in range(2):  # DEBUG re-runs the newest migration
        async with test_engine.begin() as conn:
            await m237.upgrade(conn)

    async with test_engine.connect() as conn:
        upgraded = await conn.run_sync(_schema)
    assert upgraded == fresh

    columns, indexes, uniques, fks, pk = upgraded
    assert pk == ["id"]
    assert uniques == {"uq_archive_media_assets_asset_uuid": ["asset_uuid"]}
    assert fks == {("print_archives", ("archive_id",), "CASCADE")}
    assert indexes["uq_archive_media_assets_primary"][:2] == (True, ["archive_id"])
    assert "primary" in indexes["uq_archive_media_assets_primary"][2]
    assert indexes["uq_archive_media_assets_remote_claim"][:2] == (
        True,
        ["remote_printer_identity", "remote_storage", "remote_path", "content_sha256"],
    )
    assert indexes["ix_archive_media_assets_archive"][:2] == (False, ["archive_id", "role"])
    assert ("created_at", "TIMESTAMP", False) in columns
    assert ("size_bytes", "BIGINT", True) in columns


async def test_postgresql_enforces_both_rules_and_removes_rows_with_their_archive(test_engine):  # noqa: F811
    a, b, c = await _archives(test_engine, 3)
    claim = ("P-1", "external", "/timelapse/v.mp4")
    async with test_engine.begin() as conn:
        await conn.execute(INSERT, _row("u1", a, sha="h1", origin="printer", claim=claim))
        await conn.execute(INSERT, _row("u2", a, role="variant", rev=2))
        await conn.execute(INSERT, _row("u3", b))
        await conn.execute(INSERT, _row("u4", c))  # two rows without a remote source never collide

    for refused in (
        _row("u5", a, rev=3),  # a second primary for the same archive
        _row("u6", b, role="variant", rev=2, sha="h1", origin="printer", claim=claim),  # the same recording again
        _row("u1", c, role="variant", rev=2),  # the same asset identity
    ):
        async with test_engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(INSERT, refused)

    async with test_engine.begin() as conn:
        # PostgreSQL runs the FK action; the code path that does it for SQLite is tested on its own.
        await conn.execute(text("DELETE FROM print_archives WHERE id = :id"), {"id": a})
        left = (
            (await conn.execute(text("SELECT asset_uuid FROM archive_media_assets ORDER BY asset_uuid")))
            .scalars()
            .all()
        )
    assert left == ["u3", "u4"]


async def test_the_seed_describes_existing_videos_on_postgresql(test_engine):  # noqa: F811
    ids = await _archives(test_engine, 3)
    async with test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE print_archives SET timelapse_path = 'archive/t/a.mp4' WHERE id = :id"), {"id": ids[0]}
        )
        await conn.execute(text("UPDATE print_archives SET timelapse_path = '' WHERE id = :id"), {"id": ids[1]})
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m237.seed(factory)
    await m237.seed(factory)

    async with test_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT archive_id, archive_identity, role, revision, relative_path, origin, content_sha256, "
                    "remote_path FROM archive_media_assets"
                )
            )
        ).all()
    assert [tuple(r) for r in rows] == [(ids[0], "ID-1", "primary", 1, "archive/t/a.mp4", "legacy", None, None)]
