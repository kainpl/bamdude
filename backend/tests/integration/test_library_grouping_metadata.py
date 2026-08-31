"""Grouping metadata for a whole selection, from the DB and nothing else.

The sequencer needs to know, for every file the operator picked, which plates
it has and what filament TYPES each plate needs — before it opens any dialog.
``/library/files/{id}/plates`` answers that per file by opening the 3MF; asking
it 60 times is the cost this endpoint removes.
"""

import json
import secrets
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from backend.app.core.auth import create_access_token

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret


async def _file(
    db_session,
    filename: str,
    metadata: dict | None,
    *,
    created_by_id: int | None = None,
    deleted_at: datetime | None = None,
):
    from backend.app.models.library import LibraryFile

    row = LibraryFile(
        filename=filename,
        file_path=f"library/{filename}",
        file_size=1024,
        file_type="gcode.3mf",
        file_metadata=metadata,
        created_by_id=created_by_id,
        deleted_at=deleted_at,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _read_own_user(async_client: AsyncClient, username: str) -> tuple[dict, int]:
    """A caller holding ``library:read_own`` and nothing wider.

    BamDude ships Operators AND Viewers with ``library:read_all`` (shared farm —
    see ``permissions.DEFAULT_GROUPS``), so the scoped path is only reachable
    through a purpose-built group. Same shape as
    ``test_ownership_read_scoping.py::_make_user``, which exists for this reason.

    Returns ``(auth headers, user id)``.
    """
    admin = {"Authorization": f"Bearer {create_access_token(data={'sub': 'test_admin'})}"}
    grp = await async_client.post(
        "/api/v1/groups/", headers=admin, json={"name": f"gm_{username}", "permissions": ["library:read_own"]}
    )
    assert grp.status_code == 201, grp.text
    user = await async_client.post(
        "/api/v1/users/",
        headers=admin,
        json={"username": username, "password": _PW, "role": "user", "group_ids": [grp.json()["id"]]},
    )
    assert user.status_code == 201, user.text
    login = await async_client.post("/api/v1/auth/login", json={"username": username, "password": _PW})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, user.json()["id"]


SLICED = {
    "sliced_for_model": "P1S",
    "nozzle_diameter": 0.6,
    "bed_type": "Textured PEI Plate",
    "plates": [
        {"index": 1, "filaments": [{"slot_id": 1, "type": "PETG", "color": "#FF0000"}]},
        {
            "index": 2,
            "filaments": [
                {"slot_id": 1, "type": "PETG", "color": "#00FF00"},
                {"slot_id": 3, "type": "PLA", "color": "#000000"},
            ],
        },
    ],
}


async def test_it_returns_one_row_per_file_with_its_plates(async_client, db_session):
    f = await _file(db_session, "badges.gcode.3mf", SLICED)

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={f.id}")

    assert resp.status_code == 200, resp.text
    (row,) = resp.json()
    assert row["file_id"] == f.id
    assert row["sliced_for_model"] == "P1S"
    assert row["nozzle_diameter"] == 0.6
    assert row["bed_type"] == "Textured PEI Plate"
    assert [p["index"] for p in row["plates"]] == [1, 2]


async def test_a_plate_reports_its_filament_types_not_its_colours(async_client, db_session):
    """⚠️ Colour is deliberately absent from the payload. It is not part of any
    grouping key, and shipping it would invite a caller to key on it."""
    f = await _file(db_session, "badges.gcode.3mf", SLICED)

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={f.id}")

    plates = resp.json()[0]["plates"]
    assert plates[0]["filament_types"] == ["PETG"]
    assert plates[1]["filament_types"] == ["PETG", "PLA"]
    assert "color" not in json.dumps(plates)


async def test_a_file_with_no_plate_metadata_comes_back_with_no_plates(async_client, db_session):
    """It must still appear in the answer. Omitting it would leave the caller
    unable to tell "not queueable" from "id I never asked about"."""
    f = await _file(db_session, "raw.stl", None)

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={f.id}")

    (row,) = resp.json()
    assert row["file_id"] == f.id
    assert row["plates"] == []


async def test_unknown_ids_are_skipped_rather_than_erroring(async_client, db_session):
    f = await _file(db_session, "badges.gcode.3mf", SLICED)

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={f.id},999999")

    assert resp.status_code == 200
    assert [r["file_id"] for r in resp.json()] == [f.id]


async def test_a_read_own_caller_gets_their_own_row_and_not_the_others(async_client, db_session):
    """The batch is FILTERED, not refused.

    ⚠️ This is the only test that enters ``_library_file_visible``'s False branch
    on the batch path — every other case here runs as admin, i.e. with
    ``can_read_all=True``, where the filter cannot say no to anything. Delete the
    ``if _library_file_visible(...)`` clause from the route and this test is what
    fails; without it the clause is the sole thing between a read_own caller and
    another user's file metadata, untested.

    Refusing the whole batch would be just as wrong as leaking: a selection can
    span owners, and the caller must still get their own rows back.
    """
    headers, uid = await _read_own_user(async_client, "gm_owner")
    _, other_uid = await _read_own_user(async_client, "gm_stranger")

    mine = await _file(db_session, "mine.gcode.3mf", SLICED, created_by_id=uid)
    theirs = await _file(db_session, "theirs.gcode.3mf", SLICED, created_by_id=other_uid)
    # Ownerless rows require READ_ALL — fail-closed, same rule as the raising half.
    ownerless = await _file(db_session, "nobody.gcode.3mf", SLICED)

    ids = f"{mine.id},{theirs.id},{ownerless.id}"
    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={ids}", headers=headers)

    assert resp.status_code == 200, resp.text
    assert [r["file_id"] for r in resp.json()] == [mine.id]


async def test_a_soft_deleted_file_is_skipped(async_client, db_session):
    """``deleted_at`` set means "in the bin", and the bin is not queueable.

    Asked as admin on purpose: with ``can_read_all=True`` the ownership arm of
    the predicate cannot be what excludes this row, so only the soft-delete
    check can — the route selects with a bare ``select(LibraryFile)`` and leans
    on the predicate for it.
    """
    live = await _file(db_session, "live.gcode.3mf", SLICED)
    binned = await _file(
        db_session, "binned.gcode.3mf", SLICED, deleted_at=datetime.now(tz=timezone.utc).replace(tzinfo=None)
    )

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={live.id},{binned.id}")

    assert resp.status_code == 200, resp.text
    assert [r["file_id"] for r in resp.json()] == [live.id]
