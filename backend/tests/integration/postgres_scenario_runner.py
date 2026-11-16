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
    archive_write_lock PostgreSQL holds the per-archive write guard until commit
    archive_file_reference_process_lock independent app processes serialize a
                       shared archive file reference until the holder releases it
    archive_attach_recovery failed attach rolls back safely; retry and two sessions keep one file
    printer_lane_admission PostgreSQL serializes two direct-start admissions for
                           one printer and rejects the loser after the commit
    analysis_wait_release a cold usage projection releases its DB transaction
                          before waiting for shared file analysis
    line_door_deadlock a line's quantity change against an issue of the same line,
                       both stopped inside their operations (WS-13 E1, Q16)
    line_set_changed   a concurrent first stock position waits on the line edit's
                       product gate, rather than changing its lock set (BL6)
    order_delete_busy  deleting an order another door is writing to refuses at once
    protocol:<a…i>     the lock protocol's scenarios under two real sessions
                       (lock_protocol_scenarios.py; WS-13 E1 T1)
    swap:<name>        our owed change_table under two real sessions: one claim, the exact
                       queue owner, every error held (swap_claim_scenarios.py; Codex r1)
    declare:<name>     the operator's swap declaration under two real sessions: declare,
                       scan, refresh of selected jobs, assignment, claim; the m203 upgrade
                       (swap_declaration_scenarios.py; T9 plan, scenarios 5-9)

Usage: python -m backend.tests.integration.postgres_scenario_runner <mode>
"""

import asyncio
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace


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


async def _e9_reads() -> dict:
    """WS-13 E9 A01–A03 — the product page's reads — on PostgreSQL (K02).

    A01 ``GET /products/{id}``: ``documents_count`` (attachments that are not pictures) and
    ``orders_count`` (DISTINCT orders with a line of the product); A03
    ``GET /products/{id}/files``: ``is_3mf`` (case aside), ``in_linked_folder`` and the linked
    ``folders``; A02 the orders list with ``product_id``: each order's ``product_lines``. The
    routes are called as the server calls them, without a request's credentials (the library's
    names are then «none» — the SQL is what this checks).
    """
    from backend.app.api.routes import products as product_routes, projects as project_routes
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.product import Product, product_files, product_folders
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.services import line_config

    def attachment(category: str, name: str, order: int) -> dict:
        return {
            "category": category,
            "filename": name,
            "original_name": name,
            "size": 10,
            "sort_order": order,
            "source": "manual",
            "source_file_id": None,
            "uploaded_at": "2026-10-05T00:00:00",
        }

    async with async_session() as db:
        folder = LibraryFolder(name="E9 folder")
        db.add(folder)
        await db.flush()
        boxed = LibraryFile(
            filename="Lamp.GCODE.3MF", file_path="library/lamp.3mf", file_size=1, file_type="gcode", folder_id=folder.id
        )
        loose = LibraryFile(filename="bracket.stl", file_path="library/bracket.stl", file_size=1, file_type="stl")
        product = Product(
            name="E9 lamp",
            attachments=[
                attachment("pictures", "shot.png", 0),
                attachment("bom_docs", "bom.pdf", 1),
                attachment("other", "notes.txt", 2),
            ],
        )
        db.add_all([boxed, loose, product])
        await db.flush()
        await db.execute(
            product_files.insert(),
            [{"product_id": product.id, "library_file_id": f.id} for f in (boxed, loose)],
        )
        await db.execute(product_folders.insert().values(product_id=product.id, library_folder_id=folder.id))
        orders = [Project(name=f"E9 order {n}", status="active") for n in range(3)]
        db.add_all(orders)
        await db.flush()
        for order, quantities in zip(orders, ([2, 3], [4], []), strict=True):
            for sort_order, quantity in enumerate(quantities):
                line = ProjectLine(
                    project_id=order.id, product_id=product.id, quantity=quantity, mode="product", sort_order=sort_order
                )
                db.add(line)
                await db.flush()
                await line_config.seed_line(db, line, choices=None, counts=None)
        await db.commit()
        ids = {"product": product.id, "boxed": boxed.id, "loose": loose.id, "folder": folder.id}
        ids["orders"] = [o.id for o in orders]

    async with async_session() as db:
        card = await product_routes.get_product(ids["product"], db, None)
        files = await product_routes.get_product_files(ids["product"], _plain_request(), db, None)
        listed = await project_routes.list_projects(
            status=None,
            customer_id=None,
            product_id=ids["product"],
            responsible_id=None,
            stage=None,
            q=None,
            sort_by=None,
            page=1,
            per_page=24,
            all=False,
            db=db,
            _=None,
        )
    items = listed["items"] if isinstance(listed, dict) else listed.items
    return {
        **ids,
        "documents_count": card.documents_count,
        "orders_count": card.orders_count,
        "files": {f.library_file_id: {"is_3mf": f.is_3mf, "in_linked_folder": f.in_linked_folder} for f in files.files},
        "folders": [{"folder_id": f.folder_id, "name": f.name, "hidden": f.hidden} for f in files.folders],
        "product_lines": {item.id: [(ref.mode, ref.quantity) for ref in (item.product_lines or [])] for item in items},
    }


async def _product_roundtrip() -> dict:
    """The round trip below with the library's file service running, as the app starts it.

    Since the shared library-file preparation (2026-09-28) an upload — and the import's
    ingest is one — goes through the local worker; the in-process tests get a test adapter
    from ``conftest``, this subprocess gets the real worker behind its broker, started and
    stopped around the scenario (the lifespan's own order: broker, then the runtime).
    """
    from backend.app.core.config import settings
    from backend.app.services.library_file_runtime import start_library_file_runtime, stop_library_file_runtime
    from backend.app.services.local_worker_broker import start_local_worker_broker, stop_local_worker_broker

    base = Path(settings.base_dir)
    await start_local_worker_broker(base)
    try:
        await start_library_file_runtime(base)
        try:
            return await _product_roundtrip_body()
        finally:
            await stop_library_file_runtime()
    finally:
        await stop_local_worker_broker()


async def _product_roundtrip_body() -> dict:
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


async def _archive_write_lock() -> dict:
    """Measure the advisory lock against two real PostgreSQL sessions."""
    from backend.app.core.database import async_session
    from backend.app.services.archive_write_scope import archive_write_scope

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_writer() -> None:
        async with async_session() as db, archive_write_scope(db, 91):
            first_entered.set()
            await release_first.wait()
            await db.commit()

    async def second_writer() -> None:
        await first_entered.wait()
        second_attempted.set()
        async with async_session() as db, archive_write_scope(db, 91):
            second_entered.set()
            await db.commit()

    first = asyncio.create_task(first_writer())
    await first_entered.wait()
    second = asyncio.create_task(second_writer())
    await second_attempted.wait()
    try:
        await asyncio.wait_for(second_entered.wait(), timeout=0.2)
    except TimeoutError:
        blocked_before_commit = True
    else:
        blocked_before_commit = False
    release_first.set()
    await asyncio.gather(first, second)
    return {"blocked_before_commit": blocked_before_commit, "second_entered": second_entered.is_set()}


async def _archive_file_reference_process_lock() -> dict:
    """Prove the shared-file guard crosses interpreter/process boundaries."""

    marker_dir = Path(os.environ["DATA_DIR"]) / "file-reference-barrier"
    marker_dir.mkdir(parents=True, exist_ok=True)
    file_path = "archive/shared-donor.gcode.3mf"
    repo_root = Path(__file__).resolve().parents[3]
    worker = "backend.tests.integration.postgres_file_scope_worker"
    env = os.environ.copy()
    holder = subprocess.Popen(
        [sys.executable, "-m", worker, "hold", str(marker_dir), file_path],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    probe = None
    try:
        for _ in range(200):
            if (marker_dir / "holder-ready").exists():
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("file-reference holder did not acquire its PostgreSQL lock")

        probe = subprocess.Popen(
            [sys.executable, "-m", worker, "probe", str(marker_dir), file_path],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(200):
            if (marker_dir / "probe-attempted").exists():
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("file-reference probe did not reach the PostgreSQL lock")

        await asyncio.sleep(0.25)
        blocked_before_release = not (marker_dir / "probe-entered").exists()
        (marker_dir / "release-holder").write_text("release", encoding="utf-8")
        holder_out, holder_err = await asyncio.to_thread(holder.communicate, timeout=60)
        probe_out, probe_err = await asyncio.to_thread(probe.communicate, timeout=60)
        if holder.returncode or probe.returncode:
            raise RuntimeError(
                "file-reference workers failed: "
                f"holder={holder.returncode} {holder_out[-1000:]} {holder_err[-1000:]}; "
                f"probe={probe.returncode} {probe_out[-1000:]} {probe_err[-1000:]}"
            )
        return {
            "blocked_before_release": blocked_before_release,
            "probe_entered_after_release": (marker_dir / "probe-entered").exists(),
        }
    finally:
        for process in (holder, probe):
            if process is not None and process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.communicate, timeout=30)


async def _printer_lane_admission() -> dict:
    """Measure the shared printer-lane guard with two real PG sessions.

    The normal process-local lock is deliberately replaced by a no-op here:
    this probe's purpose is the PostgreSQL advisory fence that protects
    independent application processes, not the easier same-event-loop path.
    """
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.printer_occupancy import (
        PrinterOccupancyConflict,
        read_queue_occupancy,
        require_direct_admission,
    )
    from backend.app.services.queue_ops import _queue_claim_locks, queue_claim_scope

    class _NoopLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    async with async_session() as setup:
        printer = Printer(
            name="lane-admission-printer",
            ip_address="10.0.0.19",
            access_code="00000000",
            serial_number="LANEADMIT1",
            model="X1C",
        )
        setup.add(printer)
        await setup.flush()
        setup.add(PrinterQueue(id=printer.id, printer_id=printer.id))
        await setup.commit()
        printer_id = printer.id

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()
    second_code: str | None = None
    original_lock = _queue_claim_locks[printer_id]
    _queue_claim_locks[printer_id] = _NoopLock()

    async def first_writer() -> None:
        async with async_session() as db, queue_claim_scope(db, printer_id):
            occupancy = await read_queue_occupancy(db, printer_id, for_update=True)
            require_direct_admission(occupancy)
            item = PrintQueueItem(queue_id=printer_id, status="printing", position=0, origin="direct")
            db.add(item)
            await db.flush()
            occupancy.queue.status = "printing"
            occupancy.queue.current_item_id = item.id
            first_entered.set()
            await release_first.wait()
            await db.commit()

    async def second_writer() -> None:
        nonlocal second_code
        await first_entered.wait()
        second_attempted.set()
        async with async_session() as db, queue_claim_scope(db, printer_id):
            second_entered.set()
            occupancy = await read_queue_occupancy(db, printer_id, for_update=True)
            try:
                require_direct_admission(occupancy)
            except PrinterOccupancyConflict as exc:
                second_code = exc.code
            await db.rollback()

    try:
        first = asyncio.create_task(first_writer())
        await first_entered.wait()
        second = asyncio.create_task(second_writer())
        await second_attempted.wait()
        try:
            await asyncio.wait_for(second_entered.wait(), timeout=0.2)
        except TimeoutError:
            blocked_before_commit = True
        else:
            blocked_before_commit = False
        release_first.set()
        await asyncio.gather(first, second)
    finally:
        _queue_claim_locks[printer_id] = original_lock

    return {
        "blocked_before_commit": blocked_before_commit,
        "second_entered": second_entered.is_set(),
        "second_code": second_code,
    }


async def _analysis_wait_release() -> dict:
    """Prove the projection worker wait does not retain a PG transaction.

    This is deliberately an integration probe, not an inspection of whether
    the code calls ``commit``: a second session writes while the first is held
    at the cache await.  It catches the regression where a large parse retained
    a pooled connection/transaction through every browser poll.
    """
    from sqlalchemy import update

    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer
    from backend.app.services import print_file_analysis
    from backend.app.services.print_file_analysis import PrintFileAnalysis
    from backend.app.services.usage_projection import compute_usage_projection

    async with async_session() as setup:
        printer = Printer(
            name="analysis-printer",
            ip_address="10.0.0.9",
            access_code="00000000",
            serial_number="ANALYSIS1",
            model="X1C",
        )
        setup.add(printer)
        await setup.flush()
        archive = PrintArchive(
            printer_id=printer.id,
            filename="analysis.gcode.3mf",
            file_path="archives/analysis.gcode.3mf",
            file_size=1,
            print_name="analysis",
            status="printing",
        )
        setup.add(archive)
        await setup.commit()
        printer_id = printer.id

    entered = asyncio.Event()
    release = asyncio.Event()
    reader_transaction_open: bool | None = None

    class _Manager:
        def get_status(self, _printer_id):
            return SimpleNamespace(state="RUNNING", layer_num=1, total_layers=1, raw_data={})

    async def held_analysis(*_args, **_kwargs):
        nonlocal reader_transaction_open
        reader_transaction_open = reader.in_transaction()
        entered.set()
        await release.wait()
        return PrintFileAnalysis([{"slot_id": 1, "used_g": 1.0}], None, {})

    original = print_file_analysis.get_print_file_analysis
    print_file_analysis.get_print_file_analysis = held_analysis
    try:
        async with async_session() as reader:
            projection = asyncio.create_task(compute_usage_projection(reader, printer_id, printer_manager=_Manager()))
            await asyncio.wait_for(entered.wait(), timeout=5)
            async with async_session() as writer:
                await writer.execute(update(Printer).where(Printer.id == printer_id).values(name="analysis-writer"))
                await asyncio.wait_for(writer.commit(), timeout=5)
            release.set()
            result = await asyncio.wait_for(projection, timeout=5)
    finally:
        print_file_analysis.get_print_file_analysis = original

    return {
        "reader_transaction_open_while_waiting": reader_transaction_open,
        "writer_completed": True,
        "projection_active": result["active"],
    }


async def _archive_attach_recovery() -> dict:
    """Exercise attach rollback, retry and two-session idempotency on real PG."""
    from backend.app.core.config import settings
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer
    from backend.app.services.archive import ArchiveService

    source = Path(settings.data_dir) / "attach-recovery.gcode.3mf"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("Metadata/slice_info.config", "<config><plate /></config>")

    async with async_session() as db:
        printer = Printer(
            name="pg-attach-recovery",
            ip_address="10.0.0.88",
            access_code="00000000",
            serial_number="PGATTACH1",
            model="P1S",
        )
        db.add(printer)
        await db.commit()
        printer_id = printer.id
        archive = PrintArchive(
            printer_id=printer_id,
            filename="attach-recovery.gcode.3mf",
            file_path="",
            file_size=0,
            print_name="Attach recovery",
            status="printing",
        )
        db.add(archive)
        await db.commit()
        archive_id = archive.id
        service = ArchiveService(db)
        failed = not await service.attach_3mf_to_archive(archive_id, source, "../escape.gcode.3mf")
        marked = await service.mark_3mf_unavailable(archive_id)
        recovered = await service.attach_3mf_to_archive(archive_id, source, "attach-recovery.gcode.3mf")

        concurrent = PrintArchive(
            printer_id=printer_id,
            filename="concurrent.gcode.3mf",
            file_path="",
            file_size=0,
            print_name="Concurrent",
            status="printing",
        )
        db.add(concurrent)
        await db.commit()
        concurrent_id = concurrent.id

    async def attach_once() -> bool:
        async with async_session() as db:
            return await ArchiveService(db).attach_3mf_to_archive(concurrent_id, source, "concurrent.gcode.3mf")

    concurrent_results = await asyncio.gather(attach_once(), attach_once())
    async with async_session() as db:
        row = await db.get(PrintArchive, archive_id)
        concurrent_row = await db.get(PrintArchive, concurrent_id)
        return {
            "failed": failed,
            "marked": marked,
            "recovered": recovered,
            "marker_cleared": row is not None and not (row.extra_data or {}).get("no_3mf_available"),
            "concurrent": concurrent_results,
            "concurrent_has_file": concurrent_row is not None and bool(concurrent_row.file_path),
            "archive_files": len(list(settings.archive_dir.rglob("*.3mf"))),
        }


def _plain_request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


class _AllowEverything:
    """The request's credentials for a door called directly: every right is held (WS-13 E13 §O).

    These scenarios measure the lock protocol; who may open a door is the rights tests' question."""

    async def check(self, _gate):
        return None, True

    async def allows(self, _gate) -> bool:
        return True


ALLOW = _AllowEverything()


async def _outcome(db, call) -> str:
    """Run one route call in ``db`` and commit: ``ok``, ``http:<status>:<detail>``,
    ``deadlock`` (SQLSTATE 40P01 only) or ``error:<type>:<sqlstate>:<text>``."""
    from fastapi import HTTPException

    from backend.tests.integration.lock_barriers import DEADLOCK_DETECTED, sqlstate

    try:
        await call()
        await db.commit()
        return "ok"
    except HTTPException as e:
        await db.rollback()
        return f"http:{e.status_code}:{e.detail}"
    except Exception as e:  # noqa: BLE001 — the scenario reports what happened, whatever it was
        await db.rollback()
        code = sqlstate(e)
        if code == DEADLOCK_DETECTED:
            return "deadlock"
        return f"error:{type(e).__name__}:{code}:{str(e)[:300]}"


async def _position_ledger_consistent(item_ids) -> bool:
    from sqlalchemy import func, select

    from backend.app.core.database import async_session
    from backend.app.models.finished_stock import StockItem, StockItemMovement

    async with async_session() as db:
        for item_id in item_ids:
            item = await db.get(StockItem, item_id)
            on_hand, reserved = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(StockItemMovement.delta_on_hand), 0),
                        func.coalesce(func.sum(StockItemMovement.delta_reserved), 0),
                    ).where(StockItemMovement.item_id == item_id)
                )
            ).one()
            if (item.on_hand, item.reserved) != (int(on_hand), int(reserved)):
                return False
    return True


async def _line_door_deadlock() -> dict:
    """Q16 (spec E1 T1b): a line's quantity change against an issue of the same line.

    A — ``PATCH …/lines/{L}`` quantity 5 → 2 on a line holding 3 ready units (no
    movement yet, so the checks pass and the reservation is rewritten). B — the issue
    dialog issuing 1 unit of L. A stops after its FIRST lock, B runs to its first,
    the coordinator waits for B's barrier or for B waiting on a lock, then both go on.
    """
    from backend.app.api.routes import projects as projects_routes
    from backend.app.core.database import async_session, engine
    from backend.app.models.customer import Customer
    from backend.app.models.product import Product
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.schemas.project import FulfilmentIn, FulfilmentLineIn, ProjectLineUpdate
    from backend.app.services import finished_stock, line_config
    from backend.tests.integration.lock_barriers import Barriers, SqlRecorder, backend_pid, first_of

    async with async_session() as setup:
        customer = Customer(name="Q16 customer")
        lamp = Product(name="Q16 lamp")
        setup.add_all([customer, lamp])
        await setup.flush()
        order = Project(name="Q16 order", status="active", customer_id=customer.id)
        setup.add(order)
        await setup.flush()
        line = ProjectLine(project_id=order.id, product_id=lamp.id, quantity=5, mode="product", sort_order=0)
        setup.add(line)
        await setup.flush()
        await line_config.seed_line(setup, line, choices=None, counts=None)
        item = await finished_stock.item_for(setup, lamp.id, {}, create=True)
        await finished_stock.receive(setup, item, 4)
        await finished_stock.reserve_for_line(setup, line, 3)
        await setup.commit()
        order_id, line_id, item_id = order.id, line.id, item.id

    barriers = Barriers()
    for name in ("lock_line", "lock_item"):
        barriers.wrap(finished_stock, name)
    recorder = SqlRecorder(engine.sync_engine)
    pid_b: asyncio.Future = asyncio.get_running_loop().create_future()
    holds: dict[str, object] = {}
    trails: dict[str, list[str]] = {}

    async def run_a() -> str:
        async with async_session() as db:
            holds["a"] = barriers.hold(db, after=1)
            await SqlRecorder.tag(db, "a")
            try:
                return await _outcome(
                    db,
                    lambda: projects_routes.update_line(
                        order_id, line_id, ProjectLineUpdate(quantity=2), db, None, creds=ALLOW
                    ),
                )
            finally:
                trails["a"] = barriers.trail(db)
                await SqlRecorder.untag(db)

    async def run_b() -> str:
        async with async_session() as db:
            holds["b"] = barriers.hold(db, after=1)
            pid_b.set_result(await backend_pid(db))
            await SqlRecorder.tag(db, "b")
            data = FulfilmentIn(lines=[FulfilmentLineIn(line_id=line_id, issue=1)])
            try:
                return await _outcome(
                    db, lambda: projects_routes.fulfil_order(order_id, data, _plain_request(), db, None, creds=ALLOW)
                )
            finally:
                trails["b"] = barriers.trail(db)
                await SqlRecorder.untag(db)

    which = "a_finished_before_its_first_lock"
    outcome_b = "not_started"
    try:
        task_a = asyncio.create_task(run_a())
        hold_a = await _hold_of(holds, "a")
        a_stopped = asyncio.ensure_future(hold_a.reached.wait())
        done, _pending = await asyncio.wait({task_a, a_stopped}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
        if a_stopped in done:
            task_b = asyncio.create_task(run_b())
            which = await first_of(await _hold_of(holds, "b"), engine, pid_b)
            barriers.release_all()
            outcome_a, outcome_b = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=60)
        else:
            a_stopped.cancel()
            outcome_a = await asyncio.wait_for(task_a, timeout=60)
    finally:
        barriers.restore()
        recorder.close()

    return {
        "which": which,
        "a": outcome_a,
        "b": outcome_b,
        "a_trail": trails.get("a", []),
        "b_trail": trails.get("b", []),
        "a_sql": [list(row) for row in recorder.log.get("a", [])],
        "consistent": await _position_ledger_consistent([item_id]),
    }


async def _line_set_changed() -> dict:
    """BL6: the product gate now fences a line edit's stock-position set.

    A — ``PATCH …/lines/{L}`` quantity 5 → 4 — stops right after it read the line's
    positions (none: its configuration has no position yet). C tries to receive
    the first units of that configuration. A already holds the product gate, so
    C must wait BEFORE creating the position, then finish after A commits. The
    old coordinator awaited C synchronously while holding A at its barrier,
    creating a test-only wait cycle once recount put that gate on quantity edits.
    """
    from sqlalchemy import func, select

    from backend.app.api.routes import projects as projects_routes
    from backend.app.core.database import async_session, engine
    from backend.app.models.finished_stock import StockItemMovement
    from backend.app.models.product import Product
    from backend.app.models.project import Project, ProjectEvent
    from backend.app.models.project_line import ProjectLine
    from backend.app.schemas.project import ProjectLineUpdate
    from backend.app.services import finished_stock, line_config
    from backend.tests.integration.lock_barriers import Barriers, backend_pid, first_of

    async with async_session() as setup:
        lamp = Product(name="BL6 lamp")
        setup.add(lamp)
        await setup.flush()
        order = Project(name="BL6 order", status="active")
        setup.add(order)
        await setup.flush()
        line = ProjectLine(project_id=order.id, product_id=lamp.id, quantity=5, mode="product", sort_order=0)
        setup.add(line)
        await setup.flush()
        await line_config.seed_line(setup, line, choices=None, counts=None)
        await setup.commit()
        order_id, line_id, lamp_id = order.id, line.id, lamp.id

    barriers = Barriers()
    barriers.wrap(finished_stock, "_line_position_ids")
    barriers.wrap(finished_stock, "product_gate")
    pid_c = asyncio.get_running_loop().create_future()
    holds: dict[str, object] = {}

    async def run_a() -> str:
        async with async_session() as db:
            holds["a"] = barriers.hold(db, after=1)
            return await _outcome(
                db,
                lambda: projects_routes.update_line(
                    order_id, line_id, ProjectLineUpdate(quantity=4), db, None, creds=ALLOW
                ),
            )

    async def run_c() -> str:
        async with async_session() as db:
            holds["c"] = barriers.hold(db, after=1)
            pid_c.set_result(await backend_pid(db))
            item = await finished_stock.item_for(db, lamp_id, {}, create=True)
            await finished_stock.receive(db, item, 2)
            await db.commit()
            return "ok"

    tasks = []
    try:
        task_a = asyncio.create_task(run_a())
        tasks.append(task_a)
        hold_a = await _hold_of(holds, "a")
        await asyncio.wait_for(hold_a.reached.wait(), timeout=15)
        task_c = asyncio.create_task(run_c())
        tasks.append(task_c)
        which = await first_of(await _hold_of(holds, "c"), engine, pid_c)
        barriers.release_all()
        outcome_a, outcome_c = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    finally:
        barriers.restore()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async with async_session() as db:
        line_after = await db.get(ProjectLine, line_id)
        movements_by_line = await db.scalar(
            select(func.count()).select_from(StockItemMovement).where(StockItemMovement.project_line_id == line_id)
        )
        events = await db.scalar(
            select(func.count()).select_from(ProjectEvent).where(ProjectEvent.project_id == order_id)
        )
        item = await finished_stock.item_for(db, lamp_id, {}, create=False)
    return {
        "a": outcome_a,
        "c": outcome_c,
        "which": which,
        "on_hand": item.on_hand,
        "quantity": line_after.quantity,
        "movements_by_line": int(movements_by_line or 0),
        "events": int(events or 0),
    }


async def _order_delete_busy() -> dict:
    """BL6: deleting an order while another door wrote a journal entry to it.

    A records a journal entry for the order and keeps its transaction open (its
    insert holds the order row in KEY SHARE); B deletes the order. B's final
    ``FOR UPDATE NOWAIT`` on the order row must refuse at once — 409, everything
    rolled back — instead of waiting while it holds the order's positions and lines.
    """
    from sqlalchemy import func, select

    from backend.app.api.routes import projects as projects_routes
    from backend.app.core.database import async_session
    from backend.app.models.product import Product
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.app.services import finished_stock, line_config, order_journal

    async with async_session() as setup:
        lamp = Product(name="BL6 delete lamp")
        setup.add(lamp)
        await setup.flush()
        order = Project(name="BL6 delete order", status="active")
        setup.add(order)
        await setup.flush()
        line = ProjectLine(project_id=order.id, product_id=lamp.id, quantity=5, mode="product", sort_order=0)
        setup.add(line)
        await setup.flush()
        await line_config.seed_line(setup, line, choices=None, counts=None)
        item = await finished_stock.item_for(setup, lamp.id, {}, create=True)
        await finished_stock.receive(setup, item, 4)
        await finished_stock.reserve_for_line(setup, line, 2)
        await setup.commit()
        order_id = order.id

    async with async_session() as a:
        await order_journal.record(a, order_id, "fields_changed", {"fields": ["note"]}, actor=None)
        await a.flush()
        async with async_session() as b:
            outcome_b = await asyncio.wait_for(
                _outcome(
                    b,
                    lambda: projects_routes.delete_project(
                        project_id=order_id, request=_plain_request(), db=b, current_user=None
                    ),
                ),
                timeout=30,
            )
        await a.commit()

    async with async_session() as db:
        still_there = await db.get(Project, order_id) is not None
        lines = await db.scalar(select(func.count()).select_from(ProjectLine).where(ProjectLine.project_id == order_id))
    return {"b": outcome_b, "order_exists": still_there, "lines": int(lines or 0)}


async def _hold_of(holds: dict, key: str):
    """The Hold a task registers at its start — polled, it appears within a tick."""
    for _ in range(500):
        if holds.get(key) is not None:
            return holds[key]
        await asyncio.sleep(0.01)
    raise TimeoutError(f"session {key} never started")


async def _main(mode: str) -> dict:
    # One event loop for the whole run. The engine is a module-level singleton
    # holding connections bound to whichever loop created them, so a second
    # ``asyncio.run`` finds them attached to a closed loop and dies inside the
    # proactor with a bare ``AttributeError: 'NoneType' has no attribute 'send'``.
    if mode.startswith("g02_schema:"):
        from backend.tests.integration.g02_pg_schema_scenarios import run

        return await run(mode.split(":", 1)[1])
    await _init()
    if mode == "print_state_recovery":
        from backend.tests.integration.print_state_recovery_scenarios import run

        return await run()
    if mode == "runtime_recovery":
        from backend.tests.integration.runtime_recovery_scenarios import run

        return await run()
    if mode == "print_recovery_swap":
        from backend.tests.integration.print_recovery_swap_pg_scenarios import scenario_integrated_lock_order

        return await scenario_integrated_lock_order()
    if mode == "seed":
        return {"seeded": await _seed()}
    if mode == "product_roundtrip":
        return await _product_roundtrip()
    if mode == "e9_reads":
        return await _e9_reads()
    if mode == "cyrillic_search":
        return await _cyrillic_search()
    if mode == "archive_write_lock":
        return await _archive_write_lock()
    if mode == "archive_file_reference_process_lock":
        return await _archive_file_reference_process_lock()
    if mode == "printer_lane_admission":
        return await _printer_lane_admission()
    if mode == "analysis_wait_release":
        return await _analysis_wait_release()
    if mode == "archive_attach_recovery":
        return await _archive_attach_recovery()
    if mode == "line_door_deadlock":
        return await _line_door_deadlock()
    if mode.startswith("protocol:"):
        from backend.tests.integration.lock_protocol_scenarios import SCENARIOS

        return await SCENARIOS[mode.split(":", 1)[1]]()
    if mode.startswith("swap:"):
        from backend.tests.integration.swap_claim_scenarios import SCENARIOS

        return await SCENARIOS[mode.split(":", 1)[1]]()
    if mode.startswith("declare:"):
        from backend.tests.integration.swap_declaration_scenarios import SCENARIOS

        return await SCENARIOS[mode.split(":", 1)[1]]()
    if mode == "line_set_changed":
        return await _line_set_changed()
    if mode == "order_delete_busy":
        return await _order_delete_busy()
    return await _report()


def main() -> None:
    print(json.dumps(asyncio.run(_main(sys.argv[1]))))


if __name__ == "__main__":
    main()
