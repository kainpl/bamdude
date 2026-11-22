"""What an archive edit made by hand may and may not change (upstream audit G04).

One archive is one physical print, so three of its fields are facts of the run
and not operator metadata:

* its **status** is changed by hand only between the terminal ones — ``printing``
  belongs to the live print, both as a target and as a start;
* its **printer** is never changed;
* its **cost** feeds order totals and statistics and is bounded like the grams.

And the free-stock shelf follows a status set by hand: an order-less print's
credit belongs to ``completed`` alone.

Beside the editor, the doors that make NEW print work from an archive — the
queue add and the re-slice — ask what Reprint asks.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import part_stock
from backend.app.services.part_stock import balances
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _patch(client, archive_id: int, body: dict, **kwargs):
    return await client.patch(f"/api/v1/archives/{archive_id}", json=body, **kwargs)


async def _stored(db_session, archive_id: int, column):
    """One column of the row as the database holds it now (the API wrote through another session)."""
    return (await db_session.execute(select(column).where(PrintArchive.id == archive_id))).scalar_one()


async def _status_of(db_session, archive_id: int) -> str:
    return await _stored(db_session, archive_id, PrintArchive.status)


# --- status ---------------------------------------------------------------------------------------------


async def test_a_finished_print_cannot_be_set_to_printing(async_client, archive_factory, printer_factory, db_session):
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="failed")

    response = await _patch(async_client, archive.id, {"status": "printing"})

    assert response.status_code == 422
    assert await _status_of(db_session, archive.id) == "failed"


@pytest.mark.parametrize("target", ["completed", "failed", "aborted", "cancelled", "stopped"])
async def test_a_running_print_keeps_its_status(async_client, archive_factory, printer_factory, db_session, target):
    """The live print owns it: closed by hand, the printer's real end would be refused."""
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="printing")

    response = await _patch(async_client, archive.id, {"status": target, "notes": "x"})

    assert response.status_code == 409
    assert await _status_of(db_session, archive.id) == "printing"
    assert await _stored(db_session, archive.id, PrintArchive.notes) != "x", "a refused edit writes nothing"


async def test_a_running_print_can_still_be_annotated(async_client, archive_factory, printer_factory):
    """A client that sends the print back as it read it changes nothing and is not refused."""
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="printing")

    response = await _patch(async_client, archive.id, {"status": "printing", "notes": "watch the first layer"})

    assert response.status_code == 200
    assert response.json()["status"] == "printing"
    assert response.json()["notes"] == "watch the first layer"


@pytest.mark.parametrize(
    ("before", "after"),
    [("failed", "completed"), ("completed", "cancelled"), ("aborted", "cancelled"), ("cancelled", "failed")],
)
async def test_terminal_statuses_are_set_as_named(async_client, archive_factory, printer_factory, before, after):
    """«Cancelled» is stored as ``cancelled`` — never as ``aborted``, which statistics count as a failure."""
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status=before)

    response = await _patch(async_client, archive.id, {"status": after})

    assert response.status_code == 200
    assert response.json()["status"] == after


# --- printer --------------------------------------------------------------------------------------------


async def test_the_printer_of_a_print_is_not_editable(async_client, archive_factory, printer_factory, db_session):
    home = await printer_factory()
    other = await printer_factory(name="Other", serial_number="OTHER0001")
    archive = await archive_factory(home.id)

    for target in (other.id, 99_999, None):
        response = await _patch(async_client, archive.id, {"printer_id": target, "notes": "moved"})
        assert response.status_code == 422, (target, response.text)

    assert await _stored(db_session, archive.id, PrintArchive.printer_id) == home.id
    assert await _stored(db_session, archive.id, PrintArchive.notes) != "moved"


async def test_the_same_printer_sent_back_is_accepted(async_client, archive_factory, printer_factory):
    printer = await printer_factory()
    archive = await archive_factory(printer.id)

    response = await _patch(async_client, archive.id, {"printer_id": printer.id, "notes": "kept"})

    assert response.status_code == 200
    assert response.json()["printer_id"] == printer.id
    assert response.json()["notes"] == "kept"


# --- cost -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cost", ["-0.01", "-500", "1000000.01", "1e300", "NaN", "Infinity", "-Infinity"])
async def test_a_cost_outside_the_bounds_is_refused(async_client, archive_factory, printer_factory, db_session, cost):
    """Sent as raw JSON: Python's reader accepts the ``NaN`` / ``Infinity`` literals, the schema must not."""
    printer = await printer_factory()
    archive = await archive_factory(printer.id, cost=1.5)

    response = await async_client.patch(
        f"/api/v1/archives/{archive.id}",
        content=f'{{"cost": {cost}}}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422, response.text
    assert await _stored(db_session, archive.id, PrintArchive.cost) == 1.5


@pytest.mark.parametrize("cost", [0, 12.5, 1_000_000, None])
async def test_a_cost_inside_the_bounds_is_stored(async_client, archive_factory, printer_factory, cost):
    printer = await printer_factory()
    archive = await archive_factory(printer.id, cost=1.5)

    response = await _patch(async_client, archive.id, {"cost": cost})

    assert response.status_code == 200
    assert response.json()["cost"] == cost


# --- the shelf follows the status ------------------------------------------------------------------------


async def _lid_print(db_session, printer_id: int, *, status: str, lids: int = 2, defective: int = 0):
    """An order-less print of ``lids`` lids of a product whose file is library file 77."""
    product = Product(name="Widget")
    db_session.add(product)
    await db_session.flush()
    lid = ProductPart(product_id=product.id, kind="printed", name="lid", name_key="lid", qty_per_unit=1, sort_order=0)
    db_session.add_all([lid, ProductPlate(product_id=product.id, library_file_id=77, plate_index=0)])
    archive = PrintArchive(
        printer_id=printer_id,
        filename="a.3mf",
        print_name="A",
        file_path="x/a.3mf",
        file_size=1,
        status=status,
        started_at=datetime.now(timezone.utc),
        library_file_id=77,
        plate_index=1,
        quantity=lids,
        defective_count=defective,
    )
    db_session.add(archive)
    await db_session.flush()
    db_session.add(
        PrintArchivePart(
            archive_id=archive.id,
            name="lid",
            name_key="lid",
            identify_ids=list(range(lids)),
            quantity=lids,
            defective=defective,
        )
    )
    await db_session.commit()
    return archive.id, product.id, lid.id


async def _movements(db_session, archive_id: int) -> list[tuple[str, int, str | None]]:
    rows = (
        await db_session.execute(
            select(ProductPartStockMovement)
            .where(ProductPartStockMovement.archive_id == archive_id)
            .order_by(ProductPartStockMovement.id)
        )
    ).scalars()
    return [(m.reason, m.delta, m.note) for m in rows]


@pytest.mark.parametrize("target", ["failed", "cancelled", "aborted"])
async def test_leaving_completed_takes_the_credit_off_the_shelf(async_client, printer_factory, db_session, target):
    printer = await printer_factory()
    archive_id, product_id, lid_id = await _lid_print(db_session, printer.id, status="completed")
    await part_stock.credit_unfiled_print(db_session, await db_session.get(PrintArchive, archive_id))
    await db_session.commit()
    assert await balances(db_session, product_id) == {lid_id: 2}

    response = await _patch(async_client, archive_id, {"status": target})

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert await balances(db_session, product_id) == {lid_id: 0}
    assert await _movements(db_session, archive_id) == [
        ("unfiled_print", 2, None),
        ("manual", -2, part_stock.NOTE_STATUS_EDITED),
    ]

    # The same edit again, and a move between two failure statuses: nothing more is written.
    assert (await _patch(async_client, archive_id, {"status": target})).status_code == 200
    assert (await _patch(async_client, archive_id, {"status": "stopped"})).status_code == 200
    db_session.expire_all()
    assert len(await _movements(db_session, archive_id)) == 2
    assert await balances(db_session, product_id) == {lid_id: 0}


async def test_entering_completed_credits_the_good_parts(async_client, printer_factory, db_session):
    """As the completion hook would: ``printed − defective``, once."""
    printer = await printer_factory()
    archive_id, product_id, lid_id = await _lid_print(db_session, printer.id, status="failed", lids=3, defective=1)
    assert await balances(db_session, product_id) == {lid_id: 0}

    response = await _patch(async_client, archive_id, {"status": "completed"})

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert await balances(db_session, product_id) == {lid_id: 2}
    assert await _movements(db_session, archive_id) == [("unfiled_print", 2, part_stock.NOTE_STATUS_EDITED)]

    assert (await _patch(async_client, archive_id, {"status": "completed"})).status_code == 200
    db_session.expire_all()
    assert len(await _movements(db_session, archive_id)) == 1
    assert await balances(db_session, product_id) == {lid_id: 2}


async def test_the_credit_follows_the_status_there_and_back(async_client, printer_factory, db_session):
    printer = await printer_factory()
    archive_id, product_id, lid_id = await _lid_print(db_session, printer.id, status="completed")
    await part_stock.credit_unfiled_print(db_session, await db_session.get(PrintArchive, archive_id))
    await db_session.commit()

    for status, expected in (("failed", 0), ("completed", 2), ("cancelled", 0), ("completed", 2)):
        assert (await _patch(async_client, archive_id, {"status": status})).status_code == 200
        db_session.expire_all()
        assert await balances(db_session, product_id) == {lid_id: expected}, status


async def test_defects_typed_with_the_status_are_counted_in_the_credit(async_client, printer_factory, db_session):
    printer = await printer_factory()
    archive_id, product_id, lid_id = await _lid_print(db_session, printer.id, status="failed", lids=3)
    row_id = (
        await db_session.execute(select(PrintArchivePart.id).where(PrintArchivePart.archive_id == archive_id))
    ).scalar_one()

    response = await _patch(
        async_client, archive_id, {"status": "completed", "parts_defective": [{"id": row_id, "defective": 2}]}
    )

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert await balances(db_session, product_id) == {lid_id: 1}


async def test_a_print_under_an_order_is_not_credited(async_client, printer_factory, db_session):
    from backend.app.models.project import Project

    printer = await printer_factory()
    archive_id, product_id, lid_id = await _lid_print(db_session, printer.id, status="failed")
    order = Project(name="Order")
    db_session.add(order)
    await db_session.flush()
    (await db_session.get(PrintArchive, archive_id)).project_id = order.id
    await db_session.commit()

    response = await _patch(async_client, archive_id, {"status": "completed"})

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert await balances(db_session, product_id) == {lid_id: 0}
    assert await _movements(db_session, archive_id) == []


async def test_stock_already_spent_does_not_refuse_the_status(async_client, printer_factory, db_session):
    """The convention of late defects: the archive is the print history; the shelf is corrected by hand."""
    printer = await printer_factory()
    archive_id, product_id, lid_id = await _lid_print(db_session, printer.id, status="completed")
    await part_stock.credit_unfiled_print(db_session, await db_session.get(PrintArchive, archive_id))
    await part_stock.move(db_session, part_id=lid_id, delta=-2, reason="manual", note="shipped")
    await db_session.commit()

    response = await _patch(async_client, archive_id, {"status": "failed"})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    db_session.expire_all()
    assert await balances(db_session, product_id) == {lid_id: 0}
    assert await _movements(db_session, archive_id) == [("unfiled_print", 2, None)]


# --- new print work from an archive asks the reprint right ----------------------------------------------

R_ALL, R_OWN = Permission.ARCHIVES_READ_ALL.value, Permission.ARCHIVES_READ_OWN.value
P_ALL, P_OWN = Permission.ARCHIVES_REPRINT_ALL.value, Permission.ARCHIVES_REPRINT_OWN.value
QUEUE, UPLOAD = Permission.QUEUE_CREATE.value, Permission.LIBRARY_UPLOAD.value
_SLICE_BODY = {"printer_preset_id": 1, "process_preset_id": 2, "filament_preset_id": 3}
_PAST_THE_GATE = "Archive source file missing on disk"


@pytest.fixture
async def desk(db_session, archive_factory, printer_factory):
    printer = await printer_factory()
    reader = await _user(db_session, "g04_reader", [R_ALL, QUEUE, UPLOAD])
    own = await _user(db_session, "g04_own", [R_ALL, P_OWN, QUEUE, UPLOAD])
    await _user(db_session, "g04_all", [R_ALL, P_ALL, QUEUE, UPLOAD])
    await _user(db_session, "g04_blind", [R_OWN, P_OWN, QUEUE, UPLOAD])
    mine = await archive_factory(printer.id, created_by_id=own.id, file_path="archives/none/mine.3mf")
    theirs = await archive_factory(printer.id, created_by_id=reader.id, file_path="archives/none/theirs.3mf")
    trashed = await archive_factory(
        printer.id, created_by_id=own.id, file_path="archives/none/gone.3mf", deleted_at=datetime.now(timezone.utc)
    )
    return {
        "printer": printer.id,
        "reader": reader,
        "own": own,
        "mine": mine.id,
        "theirs": theirs.id,
        "trashed": trashed.id,
    }


@pytest.mark.parametrize(
    ("who", "which", "status"),
    [
        ("g04_reader", "theirs", 403),  # their own print, but no reprint right at all
        ("g04_own", "theirs", 403),  # reprint_own, someone else's print
        ("g04_blind", "theirs", 404),  # cannot even see it
        ("g04_own", "trashed", 404),
        ("g04_all", "trashed", 404),
    ],
)
async def test_queueing_an_archive_asks_the_reprint_right(committing_client, desk, who, which, status):
    response = await committing_client.post(
        "/api/v1/queue/",
        json={"queue_id": desk["printer"], "archive_id": desk[which]},
        headers=await _jwt(who),
    )

    assert response.status_code == status, response.text


@pytest.mark.parametrize(("who", "which"), [("g04_own", "mine"), ("g04_all", "theirs"), ("g04_all", "mine")])
async def test_the_reprint_right_opens_the_queue_door(committing_client, desk, who, which):
    """Past the gate: whatever the queue writer answers next, it is not the gate's refusal."""
    response = await committing_client.post(
        "/api/v1/queue/",
        json={"queue_id": desk["printer"], "archive_id": desk[which]},
        headers=await _jwt(who),
    )

    assert response.status_code != 403, response.text
    assert response.json().get("detail") != "Archive not found"


@pytest.mark.parametrize(
    ("who", "which", "status", "detail"),
    [
        ("g04_reader", "theirs", 403, None),
        ("g04_own", "theirs", 403, None),
        ("g04_blind", "theirs", 404, "Archive not found"),
        ("g04_all", "trashed", 404, "Archive not found"),
        ("g04_own", "mine", 404, _PAST_THE_GATE),
        ("g04_all", "theirs", 404, _PAST_THE_GATE),
    ],
)
async def test_reslicing_an_archive_asks_the_reprint_right(committing_client, desk, who, which, status, detail):
    response = await committing_client.post(
        f"/api/v1/archives/{desk[which]}/slice", json=_SLICE_BODY, headers=await _jwt(who)
    )

    assert response.status_code == status, response.text
    if detail is not None:
        assert response.json()["detail"] == detail


async def test_an_api_key_reslices_no_more_than_its_owner_may(committing_client, desk, db_session):
    """A key used to read every archive here whatever its owner held."""
    blind = (await _user(db_session, "g04_key_blind", [R_OWN, P_OWN, UPLOAD])).id
    everything = (await _user(db_session, "g04_key_all", [R_ALL, P_ALL, UPLOAD])).id
    scopes = {"can_manage_library": True, "can_read_status": True, "can_queue": True}
    from backend.app.models.user import User

    weak = await _key(db_session, await db_session.get(User, blind), **scopes)
    strong = await _key(db_session, await db_session.get(User, everything), **scopes)
    unscoped = await _key(
        db_session,
        await db_session.get(User, everything),
        can_manage_library=True,
        can_read_status=False,
        can_queue=False,
    )
    committing_client.headers.pop("Authorization", None)

    async def slice_as(key: str):
        return await committing_client.post(
            f"/api/v1/archives/{desk['theirs']}/slice", json=_SLICE_BODY, headers={"X-API-Key": key}
        )

    refused = await slice_as(weak)
    assert (refused.status_code, refused.json()["detail"]) == (404, "Archive not found")
    assert (await slice_as(unscoped)).status_code in (403, 404)
    assert (await slice_as(unscoped)).json()["detail"] != _PAST_THE_GATE
    allowed = await slice_as(strong)
    assert (allowed.status_code, allowed.json()["detail"]) == (404, _PAST_THE_GATE)


# --- reprint defaults to the plate the archive printed ---------------------------------------------------


def _sliced_3mf(*plates: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for plate in plates:
            zf.writestr(f"Metadata/plate_{plate}.gcode", "G28\n")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("plate_index", "body", "expected"),
    [
        (3, {}, 3),  # the plate that was printed, not the first of the file
        (3, {"plate_id": 2}, 2),  # the caller's choice stands
        (7, {}, None),  # a plate the file cannot print: the dispatcher's own fallback
        (None, {}, None),
    ],
)
async def test_reprint_without_a_plate_takes_the_archives_own(
    async_client, archive_factory, printer_factory, tmp_path, plate_index, body, expected
):
    printer = await printer_factory()
    archive = await archive_factory(
        printer.id, filename="all.gcode.3mf", file_path="archives/test/all.gcode.3mf", plate_index=plate_index
    )
    archive_file = tmp_path / archive.file_path
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_bytes(_sliced_3mf(1, 2, 3))

    with (
        patch("backend.app.api.routes.archives.settings.base_dir", tmp_path),
        patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
        patch(
            "backend.app.services.background_dispatch.background_dispatch.dispatch_reprint_archive",
            new=AsyncMock(return_value={"dispatch_job_id": 1, "dispatch_position": 1}),
        ) as dispatch,
    ):
        response = await async_client.post(
            f"/api/v1/archives/{archive.id}/reprint?printer_id={printer.id}", json=body or None
        )

    assert response.status_code == 200, response.text
    assert dispatch.await_args.kwargs["options"].get("plate_id") == expected


# --- export: the end day is part of the range ------------------------------------------------------------


async def _exported_names(client, query: str, zone: str | None = "Europe/Kyiv") -> set[str]:
    headers = {"X-Client-Timezone": zone} if zone else {}
    response = await client.get(f"/api/v1/archives/export?format=csv&fields=print_name&{query}", headers=headers)
    assert response.status_code == 200, response.text
    return {line.strip() for line in response.text.splitlines()[1:] if line.strip()}


async def test_export_includes_the_end_day_in_the_callers_zone(async_client, archive_factory, printer_factory):
    """Kyiv is UTC+2 in November: the 19th runs from 22:00 UTC on the 18th to 22:00 UTC on the 19th."""
    printer = await printer_factory()
    for name, created in {
        "before": datetime(2026, 11, 18, 21, 30),
        "first-hour": datetime(2026, 11, 18, 22, 30),
        "evening": datetime(2026, 11, 19, 21, 59),
        "after": datetime(2026, 11, 19, 22, 30),
    }.items():
        await archive_factory(printer.id, print_name=name, created_at=created)

    day = await _exported_names(async_client, "date_from=2026-11-19&date_to=2026-11-19")
    assert day == {"first-hour", "evening"}

    # A full timestamp is an instant: an offset is converted, a bare one is UTC as before.
    upto = await _exported_names(async_client, "date_to=2026-11-19T00:00:00%2B02:00")
    assert upto == {"before"}
    assert await _exported_names(async_client, "date_to=2026-11-18T22:00:00") == {"before"}


@pytest.mark.parametrize("format", ["csv", "xlsx"])
async def test_archive_spreadsheets_keep_text_literal(async_client, archive_factory, printer_factory, format):
    import csv
    from io import BytesIO, StringIO

    printer = await printer_factory()
    await archive_factory(printer.id, print_name="=1+1", print_time_seconds=42)
    response = await async_client.get(
        "/api/v1/archives/export",
        params={"format": format, "fields": "print_name,print_time_seconds"},
    )
    assert response.status_code == 200, response.text
    if format == "csv":
        rows = list(csv.reader(StringIO(response.text)))
        assert rows[1] == ["'=1+1", "42"]
    else:
        from openpyxl import load_workbook

        sheet = load_workbook(BytesIO(response.content)).active
        assert (sheet["A2"].data_type, sheet["A2"].value) == ("s", "=1+1")
        assert (sheet["B2"].data_type, sheet["B2"].value) == ("n", 42)


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-40", "0000-00-00T00:00:00%2B00:00"])
async def test_export_refuses_an_unreadable_date(async_client, bad):
    response = await async_client.get(f"/api/v1/archives/export?format=csv&date_to={bad}")

    assert response.status_code == 400


# --- recalculate: a usage row without a price is not "tracked" -------------------------------------------


async def test_recalculate_prices_unpriced_usage_at_the_farm_rate(async_client, db_session, archive_factory):
    """100 g printed: 40 g from a priced spool (cost 2.00), 60 g booked without any price. The 60 g
    used to count as covered and were charged nothing."""
    from sqlalchemy import delete

    await db_session.execute(delete(Settings).where(Settings.key.in_(["default_filament_cost", "spoolman_enabled"])))
    db_session.add_all(
        [Settings(key="spoolman_enabled", value="false"), Settings(key="default_filament_cost", value="20")]
    )
    spool = Spool(material="PLA", weight_used=100)
    db_session.add(spool)
    archive = PrintArchive(
        filename="mixed.gcode.3mf",
        file_path="",
        file_size=1,
        print_name="Mixed",
        status="completed",
        filament_used_grams=100,
    )
    db_session.add(archive)
    await db_session.flush()
    db_session.add_all(
        [
            SpoolUsageHistory(spool_id=spool.id, archive_id=archive.id, print_name="Mixed", weight_used=40, cost=2),
            SpoolUsageHistory(spool_id=spool.id, archive_id=archive.id, print_name="Mixed", weight_used=60, cost=None),
        ]
    )
    await db_session.commit()

    response = await async_client.post("/api/v1/statistics/recalculate-costs")

    assert response.status_code == 200, response.text
    await db_session.refresh(archive)
    assert archive.cost == pytest.approx(2 + 60 / 1000 * 20)


# --- an unfinished print nobody measured shows one full print's cost, and says so ------------------------


async def test_the_full_print_figure_is_named_only_where_usage_was_not_measured(
    async_client, db_session, archive_factory, printer_factory
):
    printer = await printer_factory()
    spool = Spool(material="PLA", weight_used=10)
    db_session.add(spool)
    await db_session.flush()
    rows = {
        "unmeasured": await archive_factory(printer.id, print_name="unmeasured", status="failed", cost=4.5),
        "cancelled": await archive_factory(printer.id, print_name="cancelled", status="cancelled", cost=4.5),
        "measured": await archive_factory(printer.id, print_name="measured", status="failed", cost=1.25),
        "done": await archive_factory(printer.id, print_name="done", status="completed", cost=4.5),
        "running": await archive_factory(printer.id, print_name="running", status="printing", cost=4.5),
        "unpriced": await archive_factory(printer.id, print_name="unpriced", status="failed", cost=None),
    }
    ids = {name: row.id for name, row in rows.items()}
    db_session.add(SpoolUsageHistory(spool_id=spool.id, archive_id=ids["measured"], weight_used=10, cost=1.25))
    await db_session.commit()

    listed = (await async_client.get("/api/v1/archives/?per_page=50")).json()["data"]

    assert {row["print_name"] for row in listed if row["cost_is_full_print"]} == {"unmeasured", "cancelled"}
    for name, expected in (("unmeasured", True), ("measured", False), ("done", False)):
        detail = (await async_client.get(f"/api/v1/archives/{ids[name]}")).json()
        assert detail["cost_is_full_print"] is expected, name


# --- the auto-queue door asks the reprint right too ------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "which", "status"),
    [
        ("g04_reader", "theirs", 403),  # their own print, but no reprint right at all
        ("g04_own", "theirs", 403),  # reprint_own, someone else's print
        ("g04_blind", "theirs", 404),  # cannot even see it
        ("g04_all", "trashed", 404),
    ],
)
async def test_auto_queueing_an_archive_asks_the_reprint_right(committing_client, desk, who, which, status):
    response = await committing_client.post(
        "/api/v1/auto-queue/", json={"archive_id": desk[which]}, headers=await _jwt(who)
    )

    assert response.status_code == status, response.text


@pytest.mark.parametrize(("who", "which"), [("g04_own", "mine"), ("g04_all", "theirs")])
async def test_the_reprint_right_opens_the_auto_queue_door(committing_client, desk, who, which):
    """Past the gate: whatever the writer answers next, it is not the gate's refusal."""
    response = await committing_client.post(
        "/api/v1/auto-queue/", json={"archive_id": desk[which]}, headers=await _jwt(who)
    )

    assert response.status_code != 403, response.text
    assert response.json().get("detail") != "Archive not found"


# --- the list: days in the caller's zone, and what counts as failed --------------------------------------


async def _listed_names(client, query: str, zone: str | None = "Europe/Kyiv") -> set[str]:
    headers = {"X-Client-Timezone": zone} if zone else {}
    response = await client.get(f"/api/v1/archives/?per_page=200&{query}", headers=headers)
    assert response.status_code == 200, response.text
    return {row["print_name"] for row in response.json()["data"]}


async def test_the_list_cuts_days_in_the_callers_zone_end_day_included(async_client, archive_factory, printer_factory):
    """Kyiv is UTC+2 in November: the 19th runs from 22:00 UTC on the 18th to 22:00 UTC on the 19th."""
    printer = await printer_factory()
    for name, created in {
        "before": datetime(2026, 11, 18, 21, 30),
        "first-hour": datetime(2026, 11, 18, 22, 30),
        "evening": datetime(2026, 11, 19, 21, 59, 59, 999500),
        "after": datetime(2026, 11, 19, 22, 30),
    }.items():
        await archive_factory(printer.id, print_name=name, created_at=created)

    assert await _listed_names(async_client, "date_from=2026-11-19&date_to=2026-11-19") == {"first-hour", "evening"}
    assert await _listed_names(async_client, "date_to=2026-11-18") == {"before"}
    # The same day read in UTC is another set of prints.
    assert await _listed_names(async_client, "date_from=2026-11-19&date_to=2026-11-19", zone="UTC") == {
        "evening",
        "after",
    }


async def test_a_stopped_print_is_a_failed_one_in_the_list(async_client, archive_factory, printer_factory):
    printer = await printer_factory()
    for status in ("completed", "failed", "aborted", "cancelled", "stopped"):
        await archive_factory(printer.id, print_name=status, status=status)

    assert await _listed_names(async_client, "collection=failed") == {"failed", "aborted", "cancelled", "stopped"}
    assert await _listed_names(async_client, "hide_failed=true") == {"completed"}
