"""Who may declare a source (swap-source-declaration spec r3 §4; plan T9.2 HTTP matrix).

Declaring needs BOTH the read and the update right of that source -- own or all, through the canonical ownership
gate; an API key needs both scopes; a media or download token opens nothing. An invisible source is the same 404 as a
missing one, with nothing of its hash, path or history in the answer. Rights revoked while the bytes are being read
commit nothing.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.group import Group
from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit
from backend.app.services import queue_sources
from backend.tests.integration.test_swap_declaration_service import a_3mf, sha
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

R_ALL, R_OWN = Permission.ARCHIVES_READ_ALL.value, Permission.ARCHIVES_READ_OWN.value
U_ALL, U_OWN = Permission.ARCHIVES_UPDATE_ALL.value, Permission.ARCHIVES_UPDATE_OWN.value


@pytest.fixture(autouse=True)
def clean_spool_state():
    queue_sources._reset_state()
    yield
    queue_sources._reset_state()


async def an_archive(db, path: Path, owner=None) -> int:
    raw = path.read_bytes()
    archive = PrintArchive(
        filename="plate.gcode.3mf",
        file_path=str(path),
        file_size=len(raw),
        status="completed",
        source_content_hash=sha(raw),
        content_hash=sha(raw),
        created_by_id=owner.id if owner else None,
    )
    db.add(archive)
    await db.commit()
    return archive.id


@pytest.fixture
async def desk(db_session, tmp_path):
    a_3mf(tmp_path / "mine.gcode.3mf", "mine")
    a_3mf(tmp_path / "theirs.gcode.3mf", "theirs")
    a_3mf(tmp_path / "nobody.gcode.3mf", "nobody")
    own = await _user(db_session, "sd_own", [R_OWN, U_OWN])
    every = await _user(db_session, "sd_all", [R_ALL, U_ALL])
    await _user(db_session, "sd_read_only", [R_ALL])
    await _user(db_session, "sd_update_only", [U_ALL])
    await _user(db_session, "sd_sees_all_updates_own", [R_ALL, U_OWN])
    await _user(db_session, "sd_reprint", [R_ALL, Permission.ARCHIVES_REPRINT_ALL.value])
    return {
        "own": own,
        "every": every,
        "mine": await an_archive(db_session, tmp_path / "mine.gcode.3mf", own),
        "theirs": await an_archive(db_session, tmp_path / "theirs.gcode.3mf", every),
        "nobody": await an_archive(db_session, tmp_path / "nobody.gcode.3mf"),
    }


async def inspect(client, archive_id, headers):
    return await client.post(f"/api/v1/archives/{archive_id}/swap-declaration/inspect", headers=headers)


async def put(client, archive_id, seen, headers, *, value=True):
    return await client.put(
        f"/api/v1/archives/{archive_id}/swap-declaration",
        json={
            "value": value,
            "expected_sha256": seen["sha256"],
            "expected_revision": seen["revision"],
            "operation_id": str(uuid.uuid4()),
        },
        headers=headers,
    )


@pytest.mark.parametrize(
    ("who", "row", "status"),
    [
        ("sd_own", "mine", 200),
        ("sd_own", "theirs", 404),
        ("sd_own", "nobody", 404),
        ("sd_all", "theirs", 200),
        ("sd_all", "nobody", 200),
        ("sd_read_only", "mine", 403),
        ("sd_update_only", "mine", 403),
        ("sd_reprint", "mine", 403),
        ("sd_sees_all_updates_own", "theirs", 403),  # visible, not its own to change
        ("sd_sees_all_updates_own", "nobody", 403),
    ],
)
async def test_declaring_needs_both_rights_on_this_very_source(who, row, status, committing_client, desk):
    answer = await inspect(committing_client, desk[row], await _jwt(who))

    assert answer.status_code == status, answer.text
    if status == 404:
        assert "sha256" not in answer.text and "gcode.3mf" not in answer.text


async def test_the_full_round_trip_over_http(committing_client, db_session, desk):
    seen = (await inspect(committing_client, desk["mine"], await _jwt("sd_own"))).json()
    done = await put(committing_client, desk["mine"], seen, await _jwt("sd_own"))

    assert done.status_code == 200, done.text
    history = await committing_client.get(
        f"/api/v1/archives/{desk['mine']}/swap-declaration/history", headers=await _jwt("sd_own")
    )
    assert history.status_code == 200 and [entry["new_value"] for entry in history.json()] == [True]
    hidden = await committing_client.get(
        f"/api/v1/archives/{desk['theirs']}/swap-declaration/history", headers=await _jwt("sd_own")
    )
    assert hidden.status_code == 404


async def test_a_non_bool_value_is_refused_before_anything_is_read(committing_client, desk):
    seen = (await inspect(committing_client, desk["mine"], await _jwt("sd_own"))).json()
    for value in ("true", 1, 0, None):
        answer = await committing_client.put(
            f"/api/v1/archives/{desk['mine']}/swap-declaration",
            json={
                "value": value,
                "expected_sha256": seen["sha256"],
                "expected_revision": seen["revision"],
                "operation_id": str(uuid.uuid4()),
            },
            headers=await _jwt("sd_own"),
        )
        assert answer.status_code == 422, (value, answer.text)


@pytest.mark.parametrize(("flags", "status"), [({}, 200), ({"can_manage_archives": False}, 403)])
async def test_an_api_key_needs_both_scopes(flags, status, committing_client, db_session, desk):
    key = await _key(db_session, desk["every"], **flags)

    answer = await inspect(committing_client, desk["theirs"], {"X-API-Key": key})

    assert answer.status_code == status, answer.text


async def test_a_media_token_opens_nothing(committing_client, desk):
    token = (await committing_client.post("/api/v1/auth/media-token", headers=await _jwt("sd_own"))).json()["token"]

    committing_client.headers.pop("Authorization", None)  # nothing but the media token
    answer = await committing_client.post(f"/api/v1/archives/{desk['mine']}/swap-declaration/inspect?token={token}")

    assert answer.status_code == 401


async def test_rights_revoked_while_the_bytes_are_read_commit_nothing(committing_client, db_session, desk, monkeypatch):
    seen = (await inspect(committing_client, desk["mine"], await _jwt("sd_own"))).json()
    real_capture = queue_sources.capture

    async def capture_while_the_group_loses_the_right(request):
        receipt = await real_capture(request)
        group = (await db_session.execute(select(Group).where(Group.name == "grp-sd_own"))).scalar_one()
        group.permissions = [R_OWN]
        await db_session.commit()
        return receipt

    monkeypatch.setattr(queue_sources, "capture", capture_while_the_group_loses_the_right)
    answer = await put(committing_client, desk["mine"], seen, await _jwt("sd_own"))

    assert answer.status_code == 403, answer.text
    assert (await db_session.execute(select(SwapSourceDeclarationAudit))).scalars().all() == []
    archive = await db_session.get(PrintArchive, desk["mine"], populate_existing=True)
    assert archive.swap_source_declaration is None
