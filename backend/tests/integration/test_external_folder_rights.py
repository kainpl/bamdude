"""External folders: who mounts one, and who sees what the scanner filed (G11, owner's decisions 3 and 5).

* Mounting names a directory of the HOST. That is an administrator's act
  (``settings:update``, as upstream since 0.2.4.5), not something
  ``library:upload`` — which Operators hold — should carry.
* The scanner files what it finds without an owner. Under "ownerless needs
  ``read_all``" a ``library:read_own`` user never saw a single file of a share.
  A file WITHOUT an owner that lives in an external folder is shared: every
  library reader sees it, everywhere the library answers. Changing and deleting
  it stay as they were, and an ownerless file in managed storage stays hidden.
* A scan job's state is the library reader's to ask, like the folder it scans.
"""

import secrets

import pytest
from httpx import AsyncClient

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_scan import LibraryScanJob
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret


async def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username='test_admin')}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[dict, int]:
    headers = await _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"xf_{username}", "permissions": permissions}
    )
    assert grp.status_code == 201, grp.text
    user = await async_client.post(
        "/api/v1/users/",
        headers=headers,
        json={"username": username, "password": _PW, "role": "user", "group_ids": [grp.json()["id"]]},
    )
    assert user.status_code == 201, user.text
    login = await async_client.post("/api/v1/auth/login", json={"username": username, "password": _PW})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, user.json()["id"]


# -- (a) mounting ------------------------------------------------------------


async def test_library_upload_alone_does_not_mount_a_host_directory(async_client, tmp_path):
    operator, _id = await _make_user(
        async_client, username="xf_operator", permissions=["library:read_all", "library:upload", "library:update_all"]
    )

    response = await async_client.post(
        "/api/v1/library/folders/external", headers=operator, json={"name": "nas", "external_path": str(tmp_path)}
    )

    assert response.status_code == 403, response.text
    assert (await async_client.get("/api/v1/library/folders")).json() == []


async def test_settings_update_mounts_one(async_client, tmp_path):
    manager, _id = await _make_user(
        async_client, username="xf_manager", permissions=["library:read_all", "settings:update"]
    )

    response = await async_client.post(
        "/api/v1/library/folders/external", headers=manager, json={"name": "nas", "external_path": str(tmp_path)}
    )

    assert response.status_code == 200, response.text
    assert response.json()["is_external"] is True


# -- (b) the scanner's ownerless files are shared ----------------------------


@pytest.fixture
async def share(async_client, db_session, tmp_path):
    reader, reader_id = await _make_user(
        async_client,
        username="xf_reader",
        permissions=["library:read", "library:read_own", "library:update_own", "library:delete_own", "queue:create"],
    )
    _other, other_id = await _make_user(async_client, username="xf_other", permissions=["library:read_own"])
    folder = LibraryFolder(name="nas", is_external=True, external_path=str(tmp_path))
    db_session.add(folder)
    await db_session.flush()

    def a_file(name, *, owner=None, external=True):
        return LibraryFile(
            filename=name,
            file_path=str(tmp_path / name) if external else f"library/{name}",
            file_type="stl",
            file_size=10,
            folder_id=folder.id if external else None,
            is_external=external,
            created_by_id=owner,
        )

    rows = {
        "scanned": a_file("scanned bracket.stl"),
        "claimed": a_file("claimed.stl", owner=other_id),
        "managed": a_file("managed orphan.stl", external=False),
    }
    (tmp_path / "scanned bracket.stl").write_bytes(b"solid x\nendsolid x\n")
    db_session.add_all(rows.values())
    await db_session.commit()
    return {"reader": reader, "reader_id": reader_id, "folder": folder, **rows}


async def _names(async_client, headers, query=""):
    response = await async_client.get(f"/api/v1/library/files?include_root=false{query}", headers=headers)
    assert response.status_code == 200, response.text
    return {f["filename"] for f in response.json()}


async def test_a_read_own_user_lists_the_scanned_files(async_client, share):
    assert await _names(async_client, share["reader"]) == {"scanned bracket.stl"}
    assert await _names(async_client, share["reader"], f"&folder_id={share['folder'].id}") == {"scanned bracket.stl"}


async def test_search_finds_them(async_client, share):
    assert await _names(async_client, share["reader"], "&q=bracket") == {"scanned bracket.stl"}
    assert await _names(async_client, share["reader"], "&q=orphan") == set()
    assert await _names(async_client, share["reader"], "&q=claimed") == set()


async def test_detail_and_download_open_for_them(async_client, share):
    detail = await async_client.get(f"/api/v1/library/files/{share['scanned'].id}", headers=share["reader"])
    download = await async_client.get(f"/api/v1/library/files/{share['scanned'].id}/download", headers=share["reader"])

    assert detail.status_code == 200, detail.text
    assert download.status_code == 200, download.text
    assert download.content.startswith(b"solid x")


async def test_another_users_file_and_a_managed_orphan_stay_hidden(async_client, share):
    for key in ("claimed", "managed"):
        response = await async_client.get(f"/api/v1/library/files/{share[key].id}", headers=share["reader"])
        assert response.status_code == 404, key


async def test_the_numbers_agree_with_the_list(async_client, share):
    headers = share["reader"]

    folder = (await async_client.get(f"/api/v1/library/folders/{share['folder'].id}", headers=headers)).json()
    stats = (await async_client.get("/api/v1/library/stats", headers=headers)).json()

    assert folder["file_count"] == 1
    assert stats["total_files"] == 1


async def test_notes_follow_the_file(async_client, share):
    response = await async_client.get(f"/api/v1/library/files/{share['scanned'].id}/notes", headers=share["reader"])
    assert response.status_code == 200, response.text


async def test_the_queue_door_sees_the_same_file(share, db_session):
    """Offered in the list, so not "Library file not found" at the queue."""
    from fastapi import HTTPException

    from backend.app.models.user import User
    from backend.app.services.queue_add import ensure_source_visible

    reader = await db_session.get(User, share["reader_id"])

    ensure_source_visible(reader, library_file=share["scanned"])
    for key in ("claimed", "managed"):
        with pytest.raises(HTTPException) as hidden:
            ensure_source_visible(reader, library_file=share[key])
        assert hidden.value.status_code == 404


async def test_changing_and_deleting_stay_as_they_were(async_client, share, db_session):
    """Seeing is not owning: ``update_own`` / ``delete_own`` still need the file to be the caller's."""
    file_id = share["scanned"].id

    renamed = await async_client.put(
        f"/api/v1/library/files/{file_id}", headers=share["reader"], json={"filename": "mine now.stl"}
    )
    deleted = await async_client.delete(f"/api/v1/library/files/{file_id}", headers=share["reader"])

    assert renamed.status_code == 403, renamed.text
    assert deleted.status_code == 403, deleted.text
    await db_session.refresh(share["scanned"])
    assert share["scanned"].filename == "scanned bracket.stl" and share["scanned"].deleted_at is None


# -- (c) a scan job's state ---------------------------------------------------


@pytest.fixture
async def scan_job(db_session, tmp_path):
    folder = LibraryFolder(name="nas2", is_external=True, external_path=str(tmp_path))
    db_session.add(folder)
    await db_session.flush()
    job = LibraryScanJob(folder_id=folder.id, status="failed", error=f"external path is not accessible: {tmp_path}")
    db_session.add(job)
    await db_session.commit()
    return job


async def test_the_legacy_read_right_alone_reads_no_scan_job(async_client, scan_job):
    """``library:read`` without read_own / read_all opens no folder and no file — nor a scan of one."""
    legacy, _id = await _make_user(async_client, username="xf_legacy", permissions=["library:read"])

    response = await async_client.get(f"/api/v1/library/scan-jobs/{scan_job.id}", headers=legacy)

    assert response.status_code == 403, response.text


async def test_a_library_reader_reads_a_scan_job(async_client, scan_job):
    reader, _id = await _make_user(async_client, username="xf_reader2", permissions=["library:read_own"])

    response = await async_client.get(f"/api/v1/library/scan-jobs/{scan_job.id}", headers=reader)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"


async def test_a_job_whose_folder_is_gone_is_a_404(async_client, db_session, scan_job):
    """SQLite runs no FK action: the job row outlives its folder."""
    from sqlalchemy import delete

    await db_session.execute(delete(LibraryFolder).where(LibraryFolder.id == scan_job.folder_id))
    await db_session.commit()

    response = await async_client.get(f"/api/v1/library/scan-jobs/{scan_job.id}")

    assert response.status_code == 404, response.text
