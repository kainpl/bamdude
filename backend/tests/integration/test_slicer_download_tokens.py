"""Open-in-slicer downloads: reachable without a session, and redeemable for their whole TTL.

A protocol handler (``bambustudioopen://``, ``orcaslicer://``) cannot send an
Authorization header, so these three routes carry a short-lived token in the
path. Two faults, both upstream #3029 (``b9bd3128``):

* **``/source-dl/`` was never reachable.** The auth middleware opens these
  downloads by an anchored pattern per route, and there was a pattern for
  ``/dl/`` but none for ``/source-dl/`` — so the slicer's header-less request
  was answered 401 before the route's own token check ran. "Open source 3MF in
  slicer" could not work at all.
* **The token was spent by the first request.** The slicer is a separate
  process we do not control: Bambu Studio's downloader retries a failed
  attempt, transfers get resumed, an on-access scanner fetches. The first fetch
  won and the slicer got a 403. The token now stays valid for its five minutes;
  resource binding and expiry are unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.core.config import settings


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "base_dir", data)
    monkeypatch.setattr(settings, "archive_dir", data / "archive")
    return data


def _write(path, body: bytes = b"3mf-bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


async def _token(kind: str, resource_id: int) -> str:
    from backend.app.core.auth import create_slicer_download_token

    return await create_slicer_download_token(kind, resource_id)


@pytest.fixture
async def archive_with_files(printer_factory, archive_factory, data_dir):
    printer = await printer_factory()
    archive = await archive_factory(
        printer.id,
        file_path="archive/1/20260924_job/job.gcode.3mf",
        source_3mf_path="archive/1/20260924_job/source/job.3mf",
    )
    _write(data_dir / "archive/1/20260924_job/job.gcode.3mf")
    _write(data_dir / "archive/1/20260924_job/source/job.3mf")
    return archive


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_source_3mf_is_reachable_without_a_session(async_client, archive_with_files):
    async_client.headers.pop("Authorization", None)
    token = await _token("source", archive_with_files.id)

    response = await async_client.get(f"/api/v1/archives/{archive_with_files.id}/source-dl/{token}/job.3mf")

    assert response.status_code == 200, response.text
    assert response.content == b"3mf-bytes"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_bad_source_token_still_meets_the_routes_own_check(async_client, archive_with_files):
    """Reachable is not open: the route's token check answers, with a 403."""
    async_client.headers.pop("Authorization", None)

    response = await async_client.get(f"/api/v1/archives/{archive_with_files.id}/source-dl/forged/job.3mf")

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("kind", "segment", "name"), [("archive", "dl", "job.gcode.3mf"), ("source", "source-dl", "job.3mf")]
)
async def test_an_archive_download_can_be_fetched_again_within_its_ttl(
    async_client, archive_with_files, kind, segment, name
):
    async_client.headers.pop("Authorization", None)
    token = await _token(kind, archive_with_files.id)
    url = f"/api/v1/archives/{archive_with_files.id}/{segment}/{token}/{name}"

    first = await async_client.get(url)
    retry = await async_client.get(url)

    assert (first.status_code, retry.status_code) == (200, 200)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_library_download_can_be_fetched_again_within_its_ttl(async_client, db_session, data_dir):
    from backend.app.models.library import LibraryFile

    lib = LibraryFile(
        filename="part.3mf", file_path="library/files/part.3mf", file_type="3mf", file_size=9, file_hash="a" * 64
    )
    db_session.add(lib)
    await db_session.commit()
    await db_session.refresh(lib)
    _write(data_dir / "library/files/part.3mf")
    async_client.headers.pop("Authorization", None)
    token = await _token("library", lib.id)
    url = f"/api/v1/library/files/{lib.id}/dl/{token}/part.3mf"

    first = await async_client.get(url)
    retry = await async_client.get(url)

    assert (first.status_code, retry.status_code) == (200, 200)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_token_still_opens_only_its_own_resource(async_client, archive_with_files):
    """Reuse inside the TTL is the only thing that changed: an archive token
    does not open the source download, nor the other way round."""
    async_client.headers.pop("Authorization", None)
    archive_token = await _token("archive", archive_with_files.id)

    response = await async_client.get(f"/api/v1/archives/{archive_with_files.id}/source-dl/{archive_token}/job.3mf")

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_default_redemption_still_spends_the_token(async_client, archive_with_files):
    """No route uses it today — every slicer download passes ``single_use=False``
    — but the default stays the safe one for the next caller: a download that
    is itself consumed must not be redeemable twice."""
    from backend.app.core.auth import verify_slicer_download_token

    token = await _token("archive", archive_with_files.id)

    assert await verify_slicer_download_token(token, "archive", archive_with_files.id) is True
    assert await verify_slicer_download_token(token, "archive", archive_with_files.id) is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_expired_token_is_refused(async_client, db_session, archive_with_files):
    from sqlalchemy import update

    from backend.app.models.auth_ephemeral import AuthEphemeralToken

    async_client.headers.pop("Authorization", None)
    token = await _token("source", archive_with_files.id)
    await db_session.execute(
        update(AuthEphemeralToken)
        .where(AuthEphemeralToken.token == token)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db_session.commit()

    response = await async_client.get(f"/api/v1/archives/{archive_with_files.id}/source-dl/{token}/job.3mf")

    assert response.status_code == 403
