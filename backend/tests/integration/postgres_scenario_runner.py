"""Runs one PostgreSQL scenario in a fresh interpreter and reports JSON.

``core.database`` builds its engine at import time from ``settings``, so the
target database cannot be switched inside a running process. Each scenario
therefore executes as a subprocess with ``DATABASE_URL`` / ``DATA_DIR`` set —
which is also exactly how the application itself starts, so the test exercises
the real path rather than a reassembled imitation.

Modes:
    seed     build a SQLite database at DATA_DIR and put a row in a few tables
    fresh    run init_db() against an empty PostgreSQL, report the schema
    migrate  run init_db() with a SQLite alongside, triggering auto-migration
    product_roundtrip  export a product to a ZIP and import it back

Usage: python -m backend.tests.integration.postgres_scenario_runner <mode>
"""

import asyncio
import json
import sys


async def _init() -> None:
    from backend.app.core.database import init_db

    await init_db()


async def _report() -> dict:
    from sqlalchemy import text

    from backend.app.core.database import Base, engine

    async with engine.begin() as conn:
        tables = [
            r[0]
            for r in (
                await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"))
            ).all()
        ]
        migrations = [
            r[0] for r in (await conn.execute(text("SELECT version FROM _migrations ORDER BY version"))).all()
        ]
        groups = [
            {"name": r[0], "permissions": len(r[1] or [])}
            for r in (await conn.execute(text("SELECT name, permissions FROM groups ORDER BY id"))).all()
        ]
        counts = {}
        for t in ("users", "printers", "print_archives", "spool", "projects"):
            if t in tables:
                counts[t] = (await conn.execute(text(f'SELECT COUNT(*) FROM "{t}"'))).scalar()  # noqa: S608

        # A sequence left behind MAX(id) is invisible until the next insert.
        lagging = []
        for t in tables:
            has_id = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t AND column_name='id'"
                    ),
                    {"t": t},
                )
            ).scalar()
            if not has_id:
                continue
            seq = (await conn.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": t})).scalar()
            if not seq:
                continue
            max_id = (await conn.execute(text(f'SELECT MAX(id) FROM "{t}"'))).scalar()  # noqa: S608
            if max_id is None:
                continue
            last = (await conn.execute(text(f"SELECT last_value FROM {seq}"))).scalar()  # noqa: S608
            if last < max_id:
                lagging.append({"table": t, "max_id": max_id, "sequence": last})

    await engine.dispose()
    return {
        "tables": tables,
        "declared": sorted(Base.metadata.tables.keys()),
        "migrations": migrations,
        "groups": groups,
        "counts": counts,
        "lagging_sequences": lagging,
    }


async def _seed() -> dict:
    """Put one row in a couple of tables so the migration has data to carry.

    Written through the ORM rather than raw SQL: several NOT NULL columns take
    their value from a Python-side ``default=``, which the database never
    supplies, so a hand-written INSERT trips over columns the model considers
    optional. Going through the session is also how the application writes.
    """
    from sqlalchemy import func, select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.models.project import Project

    async with async_session() as db:
        db.add(
            Printer(
                name="scenario-printer",
                ip_address="10.0.0.1",
                access_code="00000000",
                serial_number="SCENARIO1",
                model="X1C",
            )
        )
        db.add(Project(name="scenario-project"))
        await db.commit()

        written = {}
        for table, model in (("printers", Printer), ("projects", Project)):
            written[table] = (await db.execute(select(func.count()).select_from(model))).scalar()
    return written


async def _product_roundtrip() -> dict:
    """Export a product and import it back — against PostgreSQL, not a mock.

    The round trip is covered on SQLite by
    ``test_product_export_import.py``; this exists because the two back ends
    disagree about exactly the things this path leans on — JSON columns, a
    ``NOT NULL`` a Python-side default fills, and an ``id`` that comes from a
    sequence rather than a rowid. A 3MF is built by the SQLite test's own
    builder so the two can never drift about what a sliced file looks like.
    """
    from sqlalchemy import func, select

    from backend.app.core.config import settings
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile
    from backend.app.models.product import Product, ProductPlate
    from backend.app.services.product_card import export_zip, import_zip
    from backend.app.services.product_files import product_attachments_dir
    from backend.app.services.product_sync import sync_product_for_file
    from backend.tests.integration.test_product_export_import import sliced_3mf

    def _meta(objects_by_plate: dict[int, list[str]]) -> dict:
        return {
            "plates": [
                {
                    "index": index,
                    "printable_objects": {str(100 * index + n): name for n, name in enumerate(names, start=1)},
                    "print_time_seconds": 600,
                }
                for index, names in sorted(objects_by_plate.items())
            ]
        }

    kept_objects = {1: ["shade.stl"]}
    gone_objects = {1: ["hook.stl", "hook.stl"], 2: ["clip.stl"]}
    library = settings.base_dir / "library"
    library.mkdir(parents=True, exist_ok=True)

    async with async_session() as db:
        rows = {}
        for name, objects, marker in (
            ("kept.gcode.3mf", kept_objects, b"kept"),
            ("gone.gcode.3mf", gone_objects, b"gone"),
        ):
            (library / name).write_bytes(sliced_3mf(objects, marker=marker))
            row = LibraryFile(
                filename=name,
                file_path=f"library/{name}",
                file_size=(library / name).stat().st_size,
                file_type="gcode",
                file_metadata=_meta(objects),
            )
            db.add(row)
            rows[name] = row
        product = Product(name="Desk Lamp", designer="Chef&koch", design_id="1234567")
        db.add(product)
        await db.flush()

        for row in rows.values():
            await sync_product_for_file(db, library_file_id=row.id, product_ids=[product.id])

        directory = product_attachments_dir(product.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nshot")
        product.attachments = [
            {
                "category": "pictures",
                "filename": "shot.png",
                "original_name": "shot.png",
                "size": 12,
                "sort_order": 0,
                "source": "manual",
            }
        ]
        product.cover_image_filename = "shot.png"
        await db.commit()
        await db.refresh(product, ["parts", "plates", "library_files", "library_folders"])

        data, filename = await export_zip(db, product)
        before = (await db.execute(select(func.count()).select_from(LibraryFile))).scalar()

        # Destroy the source: the product goes, and ONE of the two files goes
        # with it. The survivor must be matched by hash; the other re-ingested.
        gone_id = rows["gone.gcode.3mf"].id
        await db.execute(ProductPlate.__table__.delete().where(ProductPlate.library_file_id == gone_id))
        await db.execute(
            Product.metadata.tables["product_files"]
            .delete()
            .where(Product.metadata.tables["product_files"].c.library_file_id == gone_id)
        )
        await db.delete(await db.get(LibraryFile, gone_id))
        await db.delete(await db.get(Product, product.id))
        await db.commit()

        imported, warnings = await import_zip(db, data, folder_id=None, user=None)
        await db.commit()
        await db.refresh(imported, ["parts", "plates", "library_files"])

        plates = sorted(
            (row.filename, plate.plate_index)
            for plate in imported.plates
            for row in [await db.get(LibraryFile, plate.library_file_id)]
        )
        return {
            "filename": filename,
            "name": imported.name,
            "designer": imported.designer,
            "design_id": imported.design_id,
            "parts": {p.name_key: p.qty_per_unit for p in imported.parts},
            "plates": [list(p) for p in plates],
            "attachments": [(a["category"], a["original_name"], a["source"]) for a in imported.attachments or []],
            "cover_is_the_picture": bool(imported.cover_image_filename)
            and imported.cover_image_filename == (imported.attachments or [{}])[0].get("filename"),
            "warnings": warnings,
            "library_rows_before": before,
            "library_rows_after": (await db.execute(select(func.count()).select_from(LibraryFile))).scalar(),
        }


async def _main(mode: str) -> dict:
    # One event loop for the whole run. The engine is a module-level singleton
    # holding connections bound to whichever loop created them, so a second
    # ``asyncio.run`` finds them attached to a closed loop and dies inside the
    # proactor with a bare ``AttributeError: 'NoneType' has no attribute 'send'``.
    await _init()
    if mode == "seed":
        return {"seeded": await _seed()}
    if mode == "product_roundtrip":
        return await _product_roundtrip()
    return await _report()


def main() -> None:
    print(json.dumps(asyncio.run(_main(sys.argv[1]))))


if __name__ == "__main__":
    main()
