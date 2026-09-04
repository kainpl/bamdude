"""A direct print takes the same DB claim a queued one does.

Print now used to claim nothing until the printer reported the job, which is
why the queue could dispatch over it. The row created here is what closes that
window — and ``status='printing'`` is deliberate: it is exactly the state the
scheduler's own dispatch reaches before it uploads, so the dispatch CAS
(gated on ``pending``) can never pick it up.
"""

import pytest

from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.queue_batch import claim_printer_for_direct_print


async def _queue(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    return printer, queue


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_claim_is_a_printing_row_the_scheduler_cannot_pick_up(db_session, printer_factory):
    printer, queue = await _queue(db_session, printer_factory)

    item = await claim_printer_for_direct_print(
        db_session, printer_id=printer.id, origin="direct", library_file_id=None, created_by_id=None
    )

    assert item is not None
    assert item.status == "printing", "pending would let the scheduler dispatch it a second time"
    assert item.started_at is not None
    assert item.position == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_queue_is_claimed_and_points_at_the_item(db_session, printer_factory):
    printer, queue = await _queue(db_session, printer_factory)

    item = await claim_printer_for_direct_print(db_session, printer_id=printer.id, origin="direct")

    await db_session.refresh(queue)
    assert queue.status == "printing", "this is the seed check_queue reads"
    assert queue.current_item_id == item.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_it_does_not_disturb_the_pending_ordering(db_session, printer_factory):
    """⚠️ position 0 and status printing: MAX(position) is taken over pending
    rows only, so the next queued item must still land at 1."""
    printer, queue = await _queue(db_session, printer_factory)
    await claim_printer_for_direct_print(db_session, printer_id=printer.id, origin="direct")

    from backend.app.services.queue_batch import enqueue_batch_copies

    items, _ = await enqueue_batch_copies(db_session, printer_id=printer.id, count=1)

    assert items[0].position == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_print_options_land_on_the_row(db_session, printer_factory):
    """The row is what the queue UI renders and what the dispatcher reads back."""
    printer, queue = await _queue(db_session, printer_factory)

    item = await claim_printer_for_direct_print(
        db_session,
        printer_id=printer.id,
        origin="direct",
        library_file_id=None,
        options={"plate_id": 3, "ams_mapping": [1, -1], "timelapse": True, "layer_inspect": True},
    )

    assert item.plate_id == 3
    assert item.ams_mapping == "[1, -1]"
    assert item.timelapse is True
    assert item.layer_inspect is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_owner_is_carried(db_session, printer_factory):
    """``queue:read_own`` filters on it — an ownerless row is invisible to whoever made it."""
    from backend.app.models.user import User

    printer, queue = await _queue(db_session, printer_factory)
    user = User(username="claimant")
    db_session.add(user)
    await db_session.commit()

    item = await claim_printer_for_direct_print(
        db_session, printer_id=printer.id, origin="direct", created_by_id=user.id
    )

    assert item.created_by_id == user.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_with_no_queue_row_claims_nothing(db_session, printer_factory):
    """⚠️ Returns None rather than raising: the caller must be able to carry on
    dispatching. A printer without a queue row is a broken install, not a
    reason to refuse someone's print."""
    printer = await printer_factory()

    assert await claim_printer_for_direct_print(db_session, printer_id=printer.id, origin="direct") is None


# ---- filing the print under the order's line (spec pass 7, Decision 4a) ----


async def _order_over_a_file(db_session, *, lines=1):
    """One product, one library file it prints from, and ``lines`` order lines
    of that product — all in the same material, so nothing but their count can
    tell them apart."""
    from backend.app.models.library import LibraryFile
    from backend.app.models.product import Product, ProductPlate
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine

    file = LibraryFile(
        filename="lamp.gcode.3mf",
        file_path="lamp",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "shade"},
                    "print_time_seconds": 100,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ]
        },
    )
    product = Product(name="Lamp")
    project = Project(name="O")
    db_session.add_all([file, product, project])
    await db_session.flush()
    db_session.add(ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0))
    made = []
    for i in range(lines):
        line = ProjectLine(project_id=project.id, product_id=product.id, quantity=1, material="PETG", sort_order=i)
        db_session.add(line)
        made.append(line)
    await db_session.commit()
    return file, project, made


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_print_now_files_the_line_the_order_did_not_name(db_session, printer_factory):
    """⚠️ The one queue writer that was NOT filing it. The Print dialog offers an
    order for a direct print too, and "print now" with quantity 1 skips
    ``enqueue_batch_copies`` entirely — so without this the operator answered
    the Order field and the row carried the order with no line, which is exactly
    the state pass 7 exists to end."""
    printer, _row = await _queue(db_session, printer_factory)
    file, project, lines = await _order_over_a_file(db_session)

    item = await claim_printer_for_direct_print(
        db_session,
        printer_id=printer.id,
        origin="direct",
        library_file_id=file.id,
        options={"plate_id": 1},
        project_id=project.id,
    )

    assert item.project_id == project.id
    assert item.project_line_id == lines[0].id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_two_alike_lines_leave_the_claim_unfiled_rather_than_guessing(db_session, printer_factory):
    """Two lines of one product in one material: the plate cannot tell them
    apart, and filing somebody's print against work nobody ordered is worse than
    leaving it to the plan's implicit branch, which re-asks on every read."""
    printer, _row = await _queue(db_session, printer_factory)
    file, project, _lines = await _order_over_a_file(db_session, lines=2)

    item = await claim_printer_for_direct_print(
        db_session,
        printer_id=printer.id,
        origin="direct",
        library_file_id=file.id,
        options={"plate_id": 1},
        project_id=project.id,
    )

    assert item.project_id == project.id
    assert item.project_line_id is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_line_the_caller_named_is_never_re_decided(db_session, printer_factory):
    printer, _row = await _queue(db_session, printer_factory)
    file, project, lines = await _order_over_a_file(db_session, lines=2)

    item = await claim_printer_for_direct_print(
        db_session,
        printer_id=printer.id,
        origin="direct",
        library_file_id=file.id,
        options={"plate_id": 1},
        project_id=project.id,
        project_line_id=lines[1].id,
    )

    assert item.project_line_id == lines[1].id
