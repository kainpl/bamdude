"""An archive left without its 3MF says WHY (audit D6 part 1, upstream 6564c740 / 034f8ac2).

The Archives banner said one thing for every empty archive: "the slicer did not
leave the file on the card — switch on *Store sent files on external storage*".
Wrong whenever BamDude never got to look: the printer refused the file
connection (the cleartext answer on port 990), rejected the access code, or
could not be reached. The download now leaves a reason — ``ftps_refused``,
``auth_rejected``, ``unreachable`` or ``not_found`` — beside the
``no_3mf_available`` marker, and the banner speaks to the most urgent one.

⚠️ No "internal storage" reason, deliberately (owner, 2026-09-26): we read both
storages on every printer that has both, so "the file sat in internal memory" is
never why an archive is empty — upstream's advice for it is a workaround for a
transport they do not have.
"""

from __future__ import annotations

import ssl
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import archive_download, bambu_ftp
from backend.app.services.bambu_ftp import BambuFTPClient


@pytest.fixture(autouse=True)
def _fresh_state():
    BambuFTPClient._last_connect_failure.clear()
    BambuFTPClient._cleartext_probed_at.clear()
    archive_download._failure_reasons.clear()
    yield
    BambuFTPClient._last_connect_failure.clear()
    archive_download._failure_reasons.clear()


def _connect_raising(error):
    ftp = MagicMock()
    ftp.connect.side_effect = error
    return ftp


# -- the FTP client remembers why it could not connect -------------------------


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"), "tls"),
        (TimeoutError("timed out"), "timeout"),
        (OSError("unreachable"), "network"),
    ],
)
def test_a_failed_connect_is_remembered_by_kind(error, kind):
    with (
        patch.object(bambu_ftp, "ImplicitFTP_TLS", return_value=_connect_raising(error)),
        patch.object(bambu_ftp, "_read_cleartext_reply", return_value=None),
    ):
        BambuFTPClient("10.0.0.9", "12345678").connect()
    assert BambuFTPClient.connect_failure_since("10.0.0.9", 0.0) == kind


def test_a_rejected_login_is_remembered():
    import ftplib

    ftp = MagicMock()
    ftp.login.side_effect = ftplib.error_perm("530 Login incorrect")
    with patch.object(bambu_ftp, "ImplicitFTP_TLS", return_value=ftp):
        BambuFTPClient("10.0.0.9", "12345678").connect()
    assert BambuFTPClient.connect_failure_since("10.0.0.9", 0.0) == "auth"


def test_a_successful_connect_forgets_the_failure():
    BambuFTPClient._last_connect_failure["10.0.0.9"] = ("tls", time.monotonic())
    with patch.object(bambu_ftp, "ImplicitFTP_TLS", return_value=MagicMock()):
        assert BambuFTPClient("10.0.0.9", "12345678").connect() is True
    assert BambuFTPClient.connect_failure_since("10.0.0.9", 0.0) is None


def test_an_older_failure_does_not_answer_for_a_later_attempt():
    BambuFTPClient._last_connect_failure["10.0.0.9"] = ("tls", 100.0)
    assert BambuFTPClient.connect_failure_since("10.0.0.9", 200.0) is None


# -- the download names the reason it failed -----------------------------------


def _printer():
    return MagicMock(id=7, ip_address="10.0.0.9", access_code="12345678", model="X1C", name="X1C")


async def _download(tmp_path: Path, *, connect_kind: str | None, found: bool):
    async def fake_try_paths(*_args, **_kwargs):
        if connect_kind is not None:
            BambuFTPClient._last_connect_failure["10.0.0.9"] = (connect_kind, time.monotonic())
            return False
        return found

    with (
        patch.object(archive_download, "download_file_try_paths_async", side_effect=fake_try_paths),
        patch.object(archive_download, "list_files_async", new=AsyncMock(return_value=[])),
        patch.object(archive_download, "_try_internal_storage", new=AsyncMock(return_value=None)),
        patch.object(archive_download, "get_ftp_retry_settings", new=AsyncMock(return_value=(True, 1, 0, 30))),
    ):
        return await archive_download.try_download_3mf(_printer(), "lamp", "lamp.3mf", tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "reason"),
    [("tls", "ftps_refused"), ("auth", "auth_rejected"), ("timeout", "unreachable"), ("network", "unreachable")],
)
async def test_a_refused_connection_is_the_reason(tmp_path, kind, reason):
    assert await _download(tmp_path, connect_kind=kind, found=False) is None
    assert archive_download.last_download_failure_reason(7) == reason


@pytest.mark.asyncio
async def test_a_file_on_neither_storage_is_not_found(tmp_path):
    assert await _download(tmp_path, connect_kind=None, found=False) is None
    assert archive_download.last_download_failure_reason(7) == "not_found"


@pytest.mark.asyncio
async def test_a_success_leaves_no_reason(tmp_path):
    archive_download._failure_reasons[7] = "ftps_refused"
    assert await _download(tmp_path, connect_kind=None, found=True) is not None
    assert archive_download.last_download_failure_reason(7) is None


# -- the archive keeps the reason beside its marker ----------------------------


@pytest.mark.asyncio
async def test_the_marker_carries_the_reason_and_a_reasonless_failure_drops_it(db_session, printer_factory):
    from backend.app.models.archive import PrintArchive
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="lamp.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="Lamp",
        status="printing",
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id
    service = ArchiveService(db_session)

    assert await service.mark_3mf_unavailable(archive_id, reason="ftps_refused")
    row = await db_session.get(PrintArchive, archive_id)
    assert row.extra_data["no_3mf_available"] is True
    assert row.extra_data["no_3mf_reason"] == "ftps_refused"

    # A later attempt that failed for a reason nobody knows (the file came down
    # and would not attach) must not keep the earlier, now stale, one.
    assert await service.mark_3mf_unavailable(archive_id)
    row = await db_session.get(PrintArchive, archive_id)
    assert row.extra_data["no_3mf_available"] is True
    assert "no_3mf_reason" not in row.extra_data


@pytest.mark.asyncio
async def test_a_failed_retry_records_the_reason(db_session, test_engine, printer_factory, monkeypatch):
    """The four retry triggers mark the row the same way ``on_print_start`` does."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.models.archive import PrintArchive
    from backend.app.services import archive_download_retry as retry_module

    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="lamp.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="Lamp",
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id

    async def refused(target, *_args, **_kwargs):
        archive_download._failure_reasons[target.id] = "auth_rejected"
        return None

    monkeypatch.setattr(retry_module, "async_session", async_sessionmaker(test_engine, class_=AsyncSession))
    monkeypatch.setattr(retry_module, "try_download_3mf", refused)

    assert await retry_module.archive_download_retry.retry_archive(archive_id) == "failed"

    db_session.expire_all()
    row = await db_session.get(PrintArchive, archive_id)
    assert row.extra_data["no_3mf_available"] is True
    assert row.extra_data["no_3mf_reason"] == "auth_rejected"
