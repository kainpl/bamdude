"""Derivative lifetime never replaces the resource's current read policy."""

import pytest

from backend.app.core.auth import create_slicer_download_token
from backend.app.core.config import settings
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.tests.auth_helpers import mint_test_authority


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["library", "archive", "source"])
async def test_slicer_token_rechecks_ownership_and_permissions(
    async_client, db_session, printer_factory, archive_factory, tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    domain = "library" if kind == "library" else "archives"
    group = Group(name=f"slicer-{kind}", permissions=[f"{domain}:read_own"])
    user = User(username=f"slicer-{kind}", role="user", groups=[group])
    db_session.add(user)
    await db_session.commit()
    if kind == "library":
        row = LibraryFile(
            filename="part.3mf", file_type="3mf", file_size=4, file_path="library/files/part.3mf", created_by_id=user.id
        )
        db_session.add(row)
        await db_session.commit()
        segment = f"library/files/{row.id}/dl"
    else:
        printer = await printer_factory()
        row = await archive_factory(
            printer.id, created_by_id=user.id, file_path="archive/part.3mf", source_3mf_path="archive/part.3mf"
        )
        segment = f"archives/{row.id}/{'source-dl' if kind == 'source' else 'dl'}"
    path = tmp_path / row.file_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"part")
    capture = await mint_test_authority(db_session, username=user.username)
    token = await create_slicer_download_token(kind, row.id, authority=capture)
    async_client.headers.pop("Authorization", None)
    url = f"/api/v1/{segment}/{token}/part.3mf"
    assert (await async_client.get(url)).status_code == 200
    # The session and derivative still exist; resource ownership moved meanwhile.
    row.created_by_id = None
    await db_session.commit()
    assert (await async_client.get(url)).status_code == 404
    row.created_by_id = user.id
    group.permissions = []
    await db_session.commit()
    assert (await async_client.get(url)).status_code in (403, 404)
