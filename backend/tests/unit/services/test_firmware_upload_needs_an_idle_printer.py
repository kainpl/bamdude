"""A firmware file is written to the SD card only of a printer positively seen idle.

The check at "prepare" is minutes old by the time the file has been fetched from
Bambu Lab: a job the queue dispatched meanwhile used to have ~100 MB written to
its card. The check is now asked again immediately before the first byte, and
the queue leaves the printer alone while the upload runs (upstream 770c6bfdf).
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.services import firmware_batch, firmware_update, printer_file_guard
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.firmware_update import FirmwareUploadStatus
from backend.app.services.printer_manager import printer_manager


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


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_firmware_transfer_tolerates_mqtt_silence_only_for_the_upload(
    monkeypatch, tmp_path, bulk, test_engine, printer_factory
):
    printer = await printer_factory(model="A1")
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"x")
    client = BambuMQTTClient(printer.ip_address, printer.serial_number, printer.access_code, model=printer.model)
    client.state.connected = True
    client._last_message_time = time.time() - 2 * client.STALE_TIMEOUT
    monkeypatch.setitem(printer_manager._clients, printer.id, client)
    monkeypatch.setattr(printer_file_guard, "wide_refusal", lambda printer_id: None)

    async def no_retry():
        return False, 0, 0, 30

    during_upload = []

    async def upload(ip, code, local, remote, **kwargs):
        assert kwargs["may_run"]()
        during_upload.append((client.is_stale(), client._transfers_active))
        return True

    async def download(*args, **kwargs):
        return firmware

    assert client.is_stale()
    module = firmware_batch if bulk else firmware_update
    monkeypatch.setattr(module, "get_ftp_retry_settings", no_retry)
    monkeypatch.setattr(module, "upload_file_async", upload)
    if bulk:
        monkeypatch.setattr(module, "async_session", async_sessionmaker(test_engine))
        await firmware_batch._ftp_upload(
            SimpleNamespace(printer_id=printer.id), SimpleNamespace(filename=firmware.name, path=firmware)
        )
    else:
        service = firmware_update.FirmwareUpdateService()
        monkeypatch.setattr(
            firmware_update, "get_firmware_service", lambda: SimpleNamespace(download_firmware=download)
        )
        monkeypatch.setattr(service, "_broadcast_progress", download)
        await service._do_upload(
            printer.id, printer.ip_address, printer.access_code, printer.model, target_version="01.00"
        )
        assert firmware_update.get_upload_state(printer.id).status == FirmwareUploadStatus.COMPLETE

    assert during_upload == [(False, 1)]
    assert client._transfers_active == 0
    assert not client.is_stale()
    client._transfer_ended_at = time.time() - client.STALE_TIMEOUT - 1
    assert client.is_stale()


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
