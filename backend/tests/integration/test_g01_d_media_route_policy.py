"""Final media authority repeats route visibility, without adding a new policy."""

import pytest
from httpx import AsyncClient

from backend.app.core.auth_lifetime import utcnow
from backend.app.models.library import LibraryFile

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f80f00000101000518d84e0000000049454e44ae426082"
)


async def test_archive_media_follows_the_keys_printers(
    async_client, printer_factory, archive_factory, db_session, monkeypatch, tmp_path
):
    """N62 D5: print history belongs to the printer that made it — a key limited to other printers
    reads neither the archive nor its pictures (the old boundary №7 let it)."""
    from backend.app.api.routes import archives

    (tmp_path / "thumb.png").write_bytes(PNG)
    (tmp_path / "video.mp4").write_bytes(b"v" * 4096)
    monkeypatch.setattr(archives.settings, "base_dir", tmp_path)
    allowed = await printer_factory(name="allowed")
    other = await printer_factory(name="history")
    archive = await archive_factory(other.id, thumbnail_path="thumb.png", timelapse_path="video.mp4")
    await db_session.close()
    created = await async_client.post(
        "/api/v1/api-keys/", json={"name": "scoped", "printer_ids": [allowed.id], "can_read_status": True}
    )
    assert created.status_code == 200
    headers = {"X-API-Key": created.json()["key"]}
    async with AsyncClient(transport=async_client._transport, base_url=async_client.base_url) as client:
        for suffix in ("", "/thumbnail", "/timelapse"):
            response = await client.get(f"/api/v1/archives/{archive.id}{suffix}", headers=headers)
            assert response.status_code == 403, (suffix, response.text)
        assert (await client.get(f"/api/v1/printers/{other.id}", headers=headers)).status_code == 403
        # An unlimited key still reads it.
        everyone = await async_client.post("/api/v1/api-keys/", json={"name": "all", "can_read_status": True})
        all_headers = {"X-API-Key": everyone.json()["key"]}
        assert (await client.get(f"/api/v1/archives/{archive.id}/thumbnail", headers=all_headers)).content == PNG


async def test_library_trash_thumbnail_keeps_route_preview_but_hard_delete_refuses(async_client, db_session, tmp_path):
    thumb = tmp_path / "trash.png"
    thumb.write_bytes(PNG)
    row = LibraryFile(
        filename="trash.3mf",
        file_path="trash.3mf",
        file_size=0,
        file_type="3mf",
        thumbnail_path=str(thumb),
        deleted_at=utcnow(),
    )
    db_session.add(row)
    await db_session.commit()
    ident = row.id
    await db_session.close()
    response = await async_client.get(f"/api/v1/library/files/{ident}/thumbnail")
    assert response.status_code == 200
    assert response.content == PNG
    await db_session.delete(row)
    await db_session.commit()
    assert (await async_client.get(f"/api/v1/library/files/{ident}/thumbnail")).status_code == 404
