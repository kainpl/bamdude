"""A firmware file is written to the SD card only of a printer positively seen idle.

The check at "prepare" is minutes old by the time the file has been fetched from
Bambu Lab: a job the queue dispatched meanwhile used to have ~100 MB written to
its card. The check is now asked again immediately before the first byte, and
the queue leaves the printer alone while the upload runs (upstream 770c6bfdf).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.services import firmware_batch, firmware_update, printer_file_guard
from backend.app.services.firmware_update import FirmwareUploadStatus


@pytest.fixture(autouse=True)
def clean():
    firmware_update._upload_states.clear()
    firmware_batch._uploading.clear()
    yield
    firmware_update._upload_states.clear()
    firmware_batch._uploading.clear()


@pytest.mark.parametrize(
    ("reason", "refused"),
    [
        (None, False),
        (printer_file_guard.ACTIVE_PRINT, True),
        (printer_file_guard.EXPECTED_PRINT, True),
        (printer_file_guard.STATE_UNKNOWN, True),
    ],
)
def test_only_a_positively_idle_printer_takes_firmware(monkeypatch, reason, refused):
    monkeypatch.setattr(printer_file_guard, "wide_refusal", lambda printer_id: reason)

    assert (firmware_update.upload_refusal(7) is not None) is refused
    assert firmware_update.may_upload(7)() is (not refused)


@pytest.mark.asyncio
@pytest.mark.parametrize("job_arrives", ["during_download", "during_the_wait_for_the_file_channel"])
async def test_the_single_upload_asks_again_before_the_first_byte(monkeypatch, tmp_path, job_arrives):
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"x")
    idle = {"now": True}
    asked: list[bool] = []

    async def download(model, version=None):
        if job_arrives == "during_download":
            idle["now"] = False
        return Path(firmware)

    async def upload(ip, code, local, remote, **kwargs):
        idle["now"] = False  # the job started while this upload waited its turn
        asked.append(kwargs["may_run"]())
        return False

    async def no_retry():
        return False, 0, 0, 30

    async def quiet(*args, **kwargs):
        return None

    service = firmware_update.FirmwareUpdateService()
    monkeypatch.setattr(
        firmware_update,
        "get_firmware_service",
        lambda: SimpleNamespace(download_firmware=download, get_latest_version=quiet),
    )
    monkeypatch.setattr(firmware_update, "upload_file_async", upload)
    monkeypatch.setattr(firmware_update, "get_ftp_retry_settings", no_retry)
    monkeypatch.setattr(service, "_broadcast_progress", quiet)
    monkeypatch.setattr(
        printer_file_guard, "wide_refusal", lambda printer_id: None if idle["now"] else printer_file_guard.ACTIVE_PRINT
    )

    await service._do_upload(7, "192.0.2.1", "code", "P1S", target_version="01.00")

    assert asked == ([] if job_arrives == "during_download" else [False])
    state = firmware_update.get_upload_state(7)
    assert state.status == FirmwareUploadStatus.ERROR
    assert "busy" in (state.error or "").lower()


@pytest.mark.parametrize(
    ("status", "running"),
    [
        (FirmwareUploadStatus.IDLE, False),
        (FirmwareUploadStatus.PREPARING, True),
        (FirmwareUploadStatus.DOWNLOADING, True),
        (FirmwareUploadStatus.UPLOADING, True),
        (FirmwareUploadStatus.COMPLETE, False),
        (FirmwareUploadStatus.ERROR, False),
    ],
)
def test_a_single_upload_is_in_progress_from_prepare_to_its_end(status, running):
    firmware_update.get_upload_state(7).status = status

    assert firmware_update.upload_in_progress(7) is running
    assert firmware_update.upload_in_progress(8) is False


def test_a_bulk_upload_counts_too():
    firmware_batch._uploading.add(7)

    assert firmware_update.upload_in_progress(7) is True


def test_the_queue_leaves_a_printer_alone_while_firmware_uploads(monkeypatch):
    from backend.app.services import print_scheduler

    manager = SimpleNamespace(
        is_connected=lambda printer_id: True,
        get_status=lambda printer_id: SimpleNamespace(state="IDLE"),
        is_awaiting_plate_clear=lambda printer_id: False,
    )
    monkeypatch.setattr(print_scheduler, "printer_manager", manager)
    scheduler = print_scheduler.PrintScheduler()

    assert scheduler._is_printer_idle(7) is True

    firmware_update.get_upload_state(7).status = FirmwareUploadStatus.UPLOADING

    assert scheduler._is_printer_idle(7) is False
