"""Helpers for creating queue items outside the ordinary add-to-queue route.

Two shapes live here: the grouped batch a quantity>1 direct print leaves behind,
and the single already-claimed row a direct print takes for itself. Both capture
their source once and enqueue from the copy (spec §5); the ``external`` half of
the claim is the one §2 exemption — a print BamDude never sent has no source to
copy at the moment its row is created.
"""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.schemas.calibration_mode import normalize_mode
from backend.app.services import queue_sources
from backend.app.services.filament_intake import item_source
from backend.app.services.filament_policy_write import prepare_routing
from backend.app.services.order_filing import resolve_line_id
from backend.app.services.queue_counters import set_queue_printing, update_queue_counters
from backend.app.services.queue_source_capture import (
    StagedSource,
    capture_staged,
    discard_staged,
    plan_capture,
    publish_staged,
)
from backend.app.services.queue_sources import CaptureRequest


def _item_columns(
    *,
    archive_id: int | None,
    library_file_id: int | None,
    options: dict | None,
    project_id: int | None,
    project_line_id: int | None = None,
) -> dict:
    """Dispatch options → queue-item columns.

    Reads with ``.get()`` because the caller's dict comes from
    ``model_dump(exclude_none=True)`` — an option left at its default is simply
    absent, not None.

    ⚠️ ``created_by_id`` is deliberately NOT in here. It is passed explicitly at
    each ``PrintQueueItem(...)`` site because ``test_queue_item_attribution``
    reads those sites with the AST and cannot see through a ``**`` unpack —
    hiding the owner in this dict would silence the guard for the whole module.
    """
    opts = options or {}
    bed_mode = normalize_mode(opts.get("bed_levelling", True))
    flow_mode = normalize_mode(opts.get("flow_cali", True))
    nozzle_mode = normalize_mode(opts.get("nozzle_offset_cali", True))
    ams_mapping = opts.get("ams_mapping")
    swap_events = opts.get("swap_macro_events")
    selected_macro_ids = opts.get("selected_macro_ids")
    execute_swap_macros = bool(opts.get("execute_swap_macros", False))

    return {
        "archive_id": archive_id,
        "library_file_id": library_file_id,
        "ams_mapping": json.dumps(ams_mapping) if ams_mapping else None,
        "plate_id": opts.get("plate_id"),
        "bed_levelling": bed_mode == "on",
        "bed_levelling_mode": bed_mode,
        "flow_cali": flow_mode == "on",
        "flow_cali_mode": flow_mode,
        "layer_inspect": bool(opts.get("layer_inspect", False)),
        "timelapse": bool(opts.get("timelapse", False)),
        "timelapse_storage": opts.get("timelapse_storage"),
        "use_ams": bool(opts.get("use_ams", True)),
        "nozzle_offset_cali": nozzle_mode == "on",
        "nozzle_offset_cali_mode": nozzle_mode,
        "mesh_mode_fast_check": bool(opts.get("mesh_mode_fast_check", True)),
        "gcode_injection": bool(opts.get("gcode_injection", False)),
        "execute_swap_macros": execute_swap_macros,
        "swap_macro_events": json.dumps(swap_events) if execute_swap_macros and swap_events else None,
        "selected_macro_ids": json.dumps(selected_macro_ids) if selected_macro_ids is not None else None,
        "auto_off_after": bool(opts.get("auto_off_after", False)),
        "project_id": project_id,
        "project_line_id": project_line_id,
    }


async def direct_print_capture_plan(db: AsyncSession, *, kind: str, source_id: int) -> CaptureRequest:
    """What to copy for a print BamDude is about to send itself (spec §2, §5).

    Called by ``background_dispatch._dispatch`` **before** it takes the printer's
    claim and outside its lock: a direct print is captured like any other add, and
    a stalled share must not be able to park a machine or hold up every other
    printer's dispatch while the bytes are read. ``external`` never comes here —
    that is the §2 exemption, a print BamDude did not send.

    ⚠️ ``kind`` decides which table is asked, and there is **no fall-through**
    between them. Archive ids and library ids are independent sequences, so an id
    that names no archive can perfectly well name a real, unrelated library file —
    and a lookup that tried the other table would capture a stranger's bytes and
    dispatch them under this job's name. A source that is gone is a refusal (422),
    which is how ``filament_intake.item_source`` and the claim below both read it.
    """
    archive = await db.get(PrintArchive, source_id) if kind == "reprint_archive" else None
    library_file = (
        (await db.execute(LibraryFile.active().where(LibraryFile.id == source_id))).scalar_one_or_none()
        if kind == "print_library_file"
        else None
    )
    return plan_capture(archive=archive, library_file=library_file)


async def claim_printer_for_direct_print(
    db: AsyncSession,
    *,
    printer_id: int,
    origin: Literal["direct", "external"],
    archive_id: int | None = None,
    library_file_id: int | None = None,
    options: dict | None = None,
    created_by_id: int | None = None,
    project_id: int | None = None,
    project_line_id: int | None = None,
    staged: StagedSource | None = None,
) -> PrintQueueItem | None:
    """Take the printer's queue claim for a print being dispatched right now.

    "Print now" used to claim nothing until ``on_print_start`` — i.e. until the
    printer had already started — so for the whole upload the queue saw an idle
    printer and dispatched over the job on its way. The row created here is the
    same claim the scheduler takes before its own dispatch, and it is read by
    the same ``PrinterQueue.status='printing'`` seed in ``check_queue``.

    ⚠️ ``status='printing'``, not ``pending``: the scheduler's dispatch flip is a
    compare-and-set gated on ``pending``, which is what stops this row being
    dispatched a second time. ``position=0`` keeps it out of the pending
    ordering, which is computed over pending rows only.

    A printer without a queue row gets one here (``services/printer_queues``)
    rather than dispatching unclaimed: an unclaimed print is one the completion
    cannot close and Repeat cannot re-arm (2026-09-04).

    ⚠️ ``origin`` is REQUIRED and has no default, because this one helper builds
    the claim for two different things: ``"direct"`` for the Print dialog and
    ``"external"`` for a print BamDude never sent (``mark_queue_printing_for_printer``
    calls it when it finds no row). A default here would silently make
    one of them lie, and what it feeds — the queue-completed notifications — is
    exactly the question "did anybody queue this?". Never ``"queue"``: nothing
    reaching this function was queued.

    ⚠️ ``staged`` is the capture the caller already took for a ``direct`` print
    (spec §5): the row is then written inside the publication's own transaction,
    so the claim and the blob's row are committed together — and the copy is
    already finished, which is what keeps the printer free for the length of it.
    An ``external`` print has no ``staged`` and takes no snapshot: §2's exemption.

    The caller owns the release: see ``background_dispatch``.
    """
    # Read the evidence BEFORE the publication: a 3MF parse under the
    # process-wide storage guard would serialise every other publication behind
    # this claim. Requirements come from the captured copy when there is one.
    routing, plate = (None, (options or {}).get("plate_id"))
    if origin == "direct":
        routing, plate = await prepare_routing(
            db,
            printer_id=printer_id,
            archive_id=archive_id,
            library_file_id=library_file_id,
            options=options,
            staged=staged,
        )
    columns = {**(options or {}), "plate_id": plate}

    async def build(session: AsyncSession, source: QueueSource | None) -> PrintQueueItem:
        from backend.app.services.printer_queues import ensure_printer_queue

        queue = await ensure_printer_queue(session, printer_id)

        # The order named without its line — file the line when this plate points
        # at exactly one (spec pass 7, Decision 4a), the same rule and the same
        # helper the three queue writers use. ⚠️ "Print now" with quantity 1 never
        # reaches ``enqueue_batch_copies``, so without this the one door that
        # dispatches straight to a printer was also the one door that dropped the
        # answer the operator had just given the Print dialog's Order field. An
        # explicit line is never overridden; an ambiguous plate stays NULL and the
        # plan's implicit branch re-asks on every read.
        line_id = project_line_id
        if project_id is not None and line_id is None:
            line_id = await resolve_line_id(
                session, project_id=project_id, library_file_id=library_file_id, plate_index=columns.get("plate_id")
            )

        item = PrintQueueItem(
            filament_routing=routing,
            queue_id=queue.id,
            position=0,
            status="printing",
            origin=origin,
            started_at=datetime.now(timezone.utc),
            created_by_id=created_by_id,
            queue_source_id=None if source is None else source.id,
            source_snapshot=None if source is None else queue_sources.snapshot_for(staged.receipt, source),
            **_item_columns(
                archive_id=archive_id,
                library_file_id=library_file_id,
                options=columns,
                project_id=project_id,
                project_line_id=line_id,
            ),
        )
        session.add(item)
        await session.flush()

        await set_queue_printing(session, queue.id, item.id)
        await update_queue_counters(session, queue.id)
        return item

    if staged is None:
        item = await build(db, None)
        await db.commit()
        await db.refresh(item)
        return item

    claimed: list[int] = []

    async def attach(session: AsyncSession, source: QueueSource) -> None:
        claimed.append((await build(session, source)).id)

    await publish_staged(staged, attach)
    # Re-read in the CALLER's session: the claim was written by the publication's
    # transaction, and ``background_dispatch`` reads the row's own answers off it.
    return await db.get(PrintQueueItem, claimed[0])


async def enqueue_batch_copies(
    db: AsyncSession,
    *,
    printer_id: int,
    count: int,
    archive_id: int | None = None,
    library_file_id: int | None = None,
    plate_id: int | None = None,
    ams_mapping: list[int] | None = None,
    bed_levelling: str | bool = True,
    flow_cali: str | bool = True,
    layer_inspect: bool = False,
    timelapse: bool = False,
    timelapse_storage: str | None = None,
    use_ams: bool = True,
    nozzle_offset_cali: str | bool = True,
    mesh_mode_fast_check: bool = True,
    gcode_injection: bool = False,
    execute_swap_macros: bool = False,
    swap_macro_events: list[str] | None = None,
    selected_macro_ids: list[int] | None = None,
    auto_off_after: bool = False,
    created_by_id: int | None = None,
    project_id: int | None = None,
    project_line_id: int | None = None,
    batch_id: str | None = None,
    library_file: LibraryFile | None = None,
    feed_policy: str | None = None,
    force_color_match: bool = False,
    filament_overrides: list[dict] | None = None,
    requirements_cache=None,
) -> tuple[list[PrintQueueItem], str | None]:
    """Append ``count`` identical pending items to the given printer's queue.

    Used by direct-print endpoints to queue up the extra copies after the first
    is dispatched. Returns (items, batch_id). If count <= 0, returns ([], None).

    ``library_file`` is the row for ``library_file_id`` when the caller has it —
    the print-now route validates the file before it queues anything, so filing
    the order line below would otherwise SELECT it a second time.
    """
    if count <= 0:
        return [], None

    # Resolve printer's queue FIRST. This door deliberately does not create one
    # (unlike the direct claim), and a caller that gets nothing back must not have
    # paid for a walk over the share to find that out.
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return [], None
    # Its id as a VALUE: everything below the commit belongs to another
    # transaction, and an ORM attribute reached across that commit would be a lazy
    # load on an async session the day somebody builds one with
    # ``expire_on_commit=True``.
    queue_id = queue.id

    # ⚠️ ONE capture for every copy in the batch (spec §5's fan-out rule, A01) —
    # never one per copy — taken with no transaction open, because the copy can
    # take minutes. This function committed at the end anyway; the commit here
    # only moves earlier.
    archive, library = (
        (None, library_file)
        if library_file is not None
        else await item_source(db, SimpleNamespace(archive_id=archive_id, library_file_id=library_file_id))
    )
    plan = plan_capture(archive=archive, library_file=library)
    await db.commit()
    staged = await capture_staged(plan)
    try:
        # §5 step 4 — routing and the resolved plate come from the CAPTURED bytes,
        # and an impossible plate is refused here with nothing published. Read
        # before the publication: the storage guard is process-wide.
        routing, plate_id = await prepare_routing(
            db,
            printer_id=printer_id,
            archive_id=archive_id,
            library_file_id=library_file_id,
            cache=requirements_cache,
            staged=staged,
            options={
                "plate_id": plate_id,
                "ams_mapping": ams_mapping,
                "use_ams": use_ams,
                "feed_policy": feed_policy,
                "force_color_match": force_color_match,
                "filament_overrides": filament_overrides,
            },
        )
        if batch_id is None:
            batch_id = str(uuid.uuid4())
        # Tri-state calibration → canonical mode string (accepts legacy bool from
        # existing callers). Store the bool mirror + *_mode column on each copy.
        bed_mode = normalize_mode(bed_levelling)
        flow_mode = normalize_mode(flow_cali)
        nozzle_mode = normalize_mode(nozzle_offset_cali)
        ams_mapping_json = json.dumps(ams_mapping) if ams_mapping else None
        swap_macro_events_json = json.dumps(swap_macro_events) if execute_swap_macros and swap_macro_events else None
        selected_macro_ids_json = json.dumps(selected_macro_ids) if selected_macro_ids is not None else None
        created_ids: list[int] = []

        async def attach(session: AsyncSession, source: QueueSource) -> None:
            result = await session.execute(
                select(func.max(PrintQueueItem.position))
                .where(PrintQueueItem.queue_id == queue_id)
                .where(PrintQueueItem.status == "pending")
            )
            max_pos = result.scalar() or 0

            # The order named without its line — file the line when this plate
            # points at exactly one (spec pass 7, Decision 4a). Every copy of a
            # batch is the same plate, so this is asked once for all of them. An
            # explicit line is never overridden; an ambiguous plate stays ``NULL``
            # and the plan's implicit branch re-asks on every read.
            line_id = project_line_id
            if project_id is not None and line_id is None:
                line_id = await resolve_line_id(
                    session,
                    project_id=project_id,
                    library_file_id=library_file_id,
                    plate_index=plate_id,
                    file=library_file,
                )

            items: list[PrintQueueItem] = []
            for i in range(count):
                items.append(
                    PrintQueueItem(
                        queue_source_id=source.id,
                        source_snapshot=queue_sources.snapshot_for(staged.receipt, source),
                        queue_id=queue_id,
                        archive_id=archive_id,
                        library_file_id=library_file_id,
                        ams_mapping=ams_mapping_json,
                        filament_routing=routing,
                        plate_id=plate_id,
                        bed_levelling=bed_mode == "on",
                        bed_levelling_mode=bed_mode,
                        flow_cali=flow_mode == "on",
                        flow_cali_mode=flow_mode,
                        layer_inspect=layer_inspect,
                        timelapse=timelapse,
                        timelapse_storage=timelapse_storage,
                        use_ams=use_ams,
                        nozzle_offset_cali=nozzle_mode == "on",
                        nozzle_offset_cali_mode=nozzle_mode,
                        mesh_mode_fast_check=mesh_mode_fast_check,
                        gcode_injection=gcode_injection,
                        execute_swap_macros=execute_swap_macros,
                        swap_macro_events=swap_macro_events_json,
                        selected_macro_ids=selected_macro_ids_json,
                        auto_off_after=auto_off_after,
                        position=max_pos + 1 + i,
                        status="pending",
                        batch_id=batch_id,
                        created_by_id=created_by_id,
                        project_id=project_id,
                        project_line_id=line_id,
                    )
                )
            session.add_all(items)
            await session.flush()
            created_ids.extend(item.id for item in items)
            await update_queue_counters(session, queue_id)

        await publish_staged(staged, attach)
    except BaseException:
        await discard_staged(staged)
        raise

    # Re-read in the CALLER's session: every caller goes on to use these rows.
    items = list(
        (
            await db.execute(
                select(PrintQueueItem).where(PrintQueueItem.id.in_(created_ids)).order_by(PrintQueueItem.position)
            )
        )
        .scalars()
        .all()
    )
    return items, batch_id
