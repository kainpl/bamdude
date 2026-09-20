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
    cyrillic_search    upper-case Cyrillic finds its lower-case row, naturally
                       and through the forced collated SQL

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
    import hashlib

    from sqlalchemy import func, select

    from backend.app.core.config import settings
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile
    from backend.app.models.product import Product
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
            payload = sliced_3mf(objects, marker=marker)
            (library / name).write_bytes(payload)
            row = LibraryFile(
                filename=name,
                file_path=f"library/{name}",
                file_size=len(payload),
                file_type="gcode",
                file_metadata=_meta(objects),
                # ⚠️ The hash is the WHOLE POINT of this scenario. ``store_library_upload``
                # sets it on every real arrival; a hand-built row that omits it is
                # invisible to ``find_reusable_row`` (which matches on
                # ``LibraryFile.file_hash``), so the survivor would be re-ingested as a
                # second row and the "matched by hash" assertion would fail while the
                # feature it names works perfectly.
                file_hash=hashlib.sha256(payload).hexdigest(),
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

        archive = await export_zip(db, product)
        before = (await db.execute(select(func.count()).select_from(LibraryFile))).scalar()

        # Destroy the source: the product goes, and ONE of the two files goes
        # with it. The survivor must be matched by hash; the other re-ingested.
        #
        # The pivots go through the COLLECTIONS, exactly as ``delete_product``
        # does: a core DELETE racing the ORM's own secondary DELETE leaves it
        # expecting two rows and finding one, which surfaces as a StaleDataError
        # from the flush rather than from the line that caused it.
        gone_id = rows["gone.gcode.3mf"].id
        product.library_files = []
        product.library_folders = []
        await db.flush()
        await db.delete(product)
        await db.flush()
        await db.delete(await db.get(LibraryFile, gone_id))
        await db.commit()

        try:
            imported, warnings = await import_zip(db, archive.path, folder_id=None, user=None)
        finally:
            archive.path.unlink(missing_ok=True)
        await db.commit()
        await db.refresh(imported, ["parts", "plates", "library_files"])

        plates = []
        for plate in imported.plates:
            row = await db.get(LibraryFile, plate.library_file_id)
            plates.append([row.filename, plate.plate_index])
        plates.sort()
        return {
            "filename": archive.ascii_filename,
            "display_filename": archive.filename,
            "name": imported.name,
            "designer": imported.designer,
            "design_id": imported.design_id,
            "parts": {p.name_key: p.qty_per_unit for p in imported.parts},
            "plates": plates,
            "attachments": [(a["category"], a["original_name"], a["source"]) for a in imported.attachments or []],
            "cover_is_the_picture": bool(imported.cover_image_filename)
            and imported.cover_image_filename == (imported.attachments or [{}])[0].get("filename"),
            "warnings": [w.model_dump() for w in warnings],
            "library_rows_before": before,
            "library_rows_after": (await db.execute(select(func.count()).select_from(LibraryFile))).scalar(),
        }


async def _cyrillic_search() -> dict:
    """A C-locale database must still find «Лампа» for ?q=ЛАМПА (core/case_folding.py).

    The unit tests compile the collated SQL and read its text; only a server
    answers whether the text is *right*. The maintainer's dev database is the
    shape that used to fail — ``LC_CTYPE = C``, where ``lower()``, ``ILIKE`` and
    ``to_tsvector`` all fold ASCII only — so this runs the two searches the
    feature exists for through the real engine, and reports what the probe
    decided at boot so a green assertion cannot come from a database that folds
    natively and never exercised the collation at all.

    And then it forces that path anyway: on a server that folds natively the
    natural run never compiles the collated SQL, so CI — and any modern
    PostgreSQL — would report green without the half of the feature this file
    exists to measure ever having been sent to a server. See the forced run below.
    """
    from sqlalchemy import select

    from backend.app.core import case_folding
    from backend.app.core.database import async_session, engine
    from backend.app.core.db_dialect import is_postgres
    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer
    from backend.app.models.product import Product

    async with async_session() as db:
        printer = Printer(
            name="cyrillic-printer",
            ip_address="10.0.0.2",
            access_code="00000000",
            serial_number="CYRILLIC1",
            model="X1C",
        )
        db.add(printer)
        await db.flush()
        db.add(Product(name="Лампа настільна"))
        db.add(
            PrintArchive(
                printer_id=printer.id,
                filename="kronshtein.gcode.3mf",
                file_path="archive/kronshtein.gcode.3mf",
                file_size=7,
                print_name="Кронштейн",
                source_content_hash="a" * 64,
            )
        )
        await db.commit()

    async def both_searches(db, **exec_opts) -> tuple[list[str], list[str]]:
        """The two questions the feature exists for, asked through the compiler."""
        products = (
            (await db.execute(select(Product.name).where(Product.name.ilike("%ЛАМПА%")), **exec_opts)).scalars().all()
        )
        archives = (
            (
                await db.execute(
                    select(PrintArchive.print_name).where(PrintArchive.print_name.ilike("%кронштейн%")), **exec_opts
                )
            )
            .scalars()
            .all()
        )
        return list(products), list(archives)

    # A second session, so the searches are a real round trip through the
    # compiler against committed rows — the way a request asks them — rather
    # than statements issued inside the transaction that wrote the data.
    async with async_session() as db:
        products, archives = await both_searches(db)

    # The forced run. A database that folds natively answers the two searches
    # above with stock ILIKE, which says nothing about the collated SQL; so when
    # the probe found native folding, engage the compiler's switch by hand and
    # ask again. On SQLite there is nothing to force — no collation to render,
    # and the fold is Python's — so the forced fields mirror the natural ones and
    # ``forced_collation`` stays None. On a database that already folds through a
    # collation the natural run WAS the forced one, which is why these start as
    # its results.
    forced_collation = case_folding.pg_fold_collation
    forced_products, forced_archives = products, archives
    if is_postgres() and case_folding.pg_native_folds:
        async with engine.connect() as conn:
            # The probe's own lookup (catalog-qualified, one candidate verified at
            # a time), so "what would this server fold through" is answered in
            # exactly one place.
            _ctype, candidate = await case_folding._verified_collation(conn)
        if candidate is not None:
            previous = case_folding.pg_fold_collation
            case_folding.pg_fold_collation = candidate  # ``pg_native_folds`` stays as measured
            try:
                async with async_session() as db:
                    # ⚠️ A new session is NOT a fresh compile: the compiled-SQL
                    # cache lives on the ENGINE and is keyed on the statement, not
                    # on ``pg_fold_collation`` (the warning in core/case_folding.py).
                    # Measured 2026-09-12: without a private cache the second run
                    # re-sends the first run's stock ILIKE text and this whole
                    # branch proves nothing.
                    forced_products, forced_archives = await both_searches(db, execution_options={"compiled_cache": {}})
            finally:
                case_folding.pg_fold_collation = previous
            forced_collation = candidate

    return {
        "products": products,
        "archives": archives,
        "native": case_folding.pg_native_folds,
        "collation": case_folding.pg_fold_collation,
        "forced_products": forced_products,
        "forced_archives": forced_archives,
        "forced_collation": forced_collation,
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
    if mode == "cyrillic_search":
        return await _cyrillic_search()
    return await _report()


def main() -> None:
    print(json.dumps(asyncio.run(_main(sys.argv[1]))))


if __name__ == "__main__":
    main()
