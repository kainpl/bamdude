"""Tests for the system fallback row in print_options_preferences (#1235).

A row with ``user_id IS NULL`` is the per-model default the virtual-printer
queue-receive path consults when a slicer omits the print-option flags. These
exercise the model invariants (nullable user_id, one system row per model) and
the dedicated /system route helpers against the in-memory test DB.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.api.routes.print_options_preferences import (
    delete_system_preference,
    list_system_preferences,
    upsert_system_preference,
)
from backend.app.models.print_options_preference import PrintOptionsPreference
from backend.app.schemas.print_options_preference import PrintOptionsPreferenceData

_TOGGLES = {
    "bed_levelling": True,
    "flow_cali": False,
    "layer_inspect": True,
    "timelapse": True,
    "mesh_mode_fast_check": True,
    "gcode_injection": False,
}
_PAYLOAD = PrintOptionsPreferenceData.model_validate(
    {"print_options": _TOGGLES, "swap_macros": {"execute": False, "events": []}}
)


@pytest.mark.asyncio
async def test_system_row_persists_with_null_user(db_session):
    """A NULL-user row is insertable and round-trips its options blob."""
    db_session.add(
        PrintOptionsPreference(
            user_id=None,
            printer_model="P1S",
            options={"print_options": _TOGGLES, "swap_macros": {"execute": False, "events": []}},
        )
    )
    await db_session.commit()

    row = await db_session.scalar(
        select(PrintOptionsPreference).where(
            PrintOptionsPreference.user_id.is_(None),
            PrintOptionsPreference.printer_model == "P1S",
        )
    )
    assert row is not None
    assert row.user_id is None
    assert row.options["print_options"]["layer_inspect"] is True


@pytest.mark.asyncio
async def test_one_system_row_per_model(db_session):
    """The partial unique index rejects a second NULL-user row for one model."""
    db_session.add(PrintOptionsPreference(user_id=None, printer_model="X1C", options={"print_options": _TOGGLES}))
    await db_session.commit()

    db_session.add(PrintOptionsPreference(user_id=None, printer_model="X1C", options={"print_options": _TOGGLES}))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_system_row_coexists_with_user_row(db_session):
    """A system row and a real user's row for the same model both persist —
    the composite (user_id, printer_model) unique only constrains the user row."""
    from backend.app.models.user import User

    user = User(username="op1", email="op1@example.test", password_hash="x", is_active=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    db_session.add_all(
        [
            PrintOptionsPreference(user_id=None, printer_model="A1", options={"print_options": _TOGGLES}),
            PrintOptionsPreference(user_id=user.id, printer_model="A1", options={"print_options": _TOGGLES}),
        ]
    )
    await db_session.commit()

    rows = (
        (await db_session.execute(select(PrintOptionsPreference).where(PrintOptionsPreference.printer_model == "A1")))
        .scalars()
        .all()
    )
    assert {r.user_id for r in rows} == {None, user.id}


@pytest.mark.asyncio
async def test_system_route_upsert_then_list_then_delete(db_session):
    """The /system route helpers upsert, list, and delete a system row."""
    created = await upsert_system_preference("P1S", _PAYLOAD, _=None, db=db_session)
    assert created.printer_model == "P1S"
    # Direct call returns the ORM row; .options is the raw JSON dict (Pydantic
    # coercion to PrintOptionsPreferenceResponse only happens at the HTTP layer).
    assert created.options["print_options"]["timelapse"] is True

    listed = await list_system_preferences(_=None, db=db_session)
    assert [r.printer_model for r in listed] == ["P1S"]

    # Upsert again with a different toggle → updates in place (no duplicate).
    flipped = PrintOptionsPreferenceData.model_validate(
        {"print_options": {**_TOGGLES, "timelapse": False}, "swap_macros": {"execute": False, "events": []}}
    )
    await upsert_system_preference("P1S", flipped, _=None, db=db_session)
    listed = await list_system_preferences(_=None, db=db_session)
    assert len(listed) == 1
    assert listed[0].options["print_options"]["timelapse"] is False

    await delete_system_preference("P1S", _=None, db=db_session)
    assert await list_system_preferences(_=None, db=db_session) == []


@pytest.mark.asyncio
async def test_system_route_delete_missing_404(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await delete_system_preference("NoSuchModel", _=None, db=db_session)
    assert exc.value.status_code == 404
