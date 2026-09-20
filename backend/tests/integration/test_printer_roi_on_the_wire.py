"""The saved plate-detection ROI reaches the wire.

The row keeps four flat ``plate_detection_roi_{x,y,w,h}`` columns while the
response carries one nested object. Nothing assembled them, so the field read
``null`` on every printer that had an ROI saved. ``Printer.plate_detection_roi``
is that assembly; these tests pin both directions of the round trip.
"""

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

DEFAULTS = {"x": 0.15, "y": 0.35, "w": 0.70, "h": 0.55}


async def _roi_columns(db_session, printer_id: int) -> dict[str, float | None]:
    from backend.app.models.printer import Printer

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(
                Printer.plate_detection_roi_x,
                Printer.plate_detection_roi_y,
                Printer.plate_detection_roi_w,
                Printer.plate_detection_roi_h,
            ).where(Printer.id == printer_id)
        )
    ).one()
    return dict(zip("xywh", row, strict=True))


async def _listed(async_client, printer_id: int) -> dict:
    rsp = await async_client.get("/api/v1/printers/")
    assert rsp.status_code == 200, rsp.text
    return next(p for p in rsp.json() if p["id"] == printer_id)


async def test_a_saved_roi_is_carried_by_the_list_response(async_client, printer_factory):
    printer = await printer_factory(
        plate_detection_roi_x=0.2,
        plate_detection_roi_y=0.3,
        plate_detection_roi_w=0.4,
        plate_detection_roi_h=0.5,
    )

    assert (await _listed(async_client, printer.id))["plate_detection_roi"] == {
        "x": 0.2,
        "y": 0.3,
        "w": 0.4,
        "h": 0.5,
    }


async def test_a_printer_that_never_had_an_roi_reports_null(async_client, printer_factory):
    printer = await printer_factory()

    assert (await _listed(async_client, printer.id))["plate_detection_roi"] is None


async def test_a_partially_saved_roi_is_completed_by_the_defaults(async_client, printer_factory):
    """One column set is still an ROI — the other three take the defaults the
    deleted ``from_orm_with_roi`` applied, rather than collapsing the field to null."""
    printer = await printer_factory(plate_detection_roi_x=0.42)

    assert (await _listed(async_client, printer.id))["plate_detection_roi"] == {**DEFAULTS, "x": 0.42}


async def test_a_zero_component_survives_instead_of_being_read_as_missing(async_client, printer_factory):
    """0.0 is the edge of the frame, a legitimate value — not an unset column."""
    printer = await printer_factory(plate_detection_roi_x=0.0, plate_detection_roi_y=0.0)

    assert (await _listed(async_client, printer.id))["plate_detection_roi"] == {**DEFAULTS, "x": 0.0, "y": 0.0}


async def test_patching_an_roi_writes_the_columns_and_echoes_the_object(async_client, db_session, printer_factory):
    printer = await printer_factory()
    roi = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}

    rsp = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"plate_detection_roi": roi})

    assert rsp.status_code == 200, rsp.text
    assert rsp.json()["plate_detection_roi"] == roi
    assert await _roi_columns(db_session, printer.id) == roi


async def test_patching_a_null_roi_clears_the_columns(async_client, db_session, printer_factory):
    printer = await printer_factory(
        plate_detection_roi_x=0.2,
        plate_detection_roi_y=0.3,
        plate_detection_roi_w=0.4,
        plate_detection_roi_h=0.5,
    )

    rsp = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"plate_detection_roi": None})

    assert rsp.status_code == 200, rsp.text
    assert rsp.json()["plate_detection_roi"] is None
    assert await _roi_columns(db_session, printer.id) == {"x": None, "y": None, "w": None, "h": None}
