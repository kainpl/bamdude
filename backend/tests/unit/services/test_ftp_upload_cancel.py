"""Cancel during an upload ends the upload -- whatever the printer says about the aborted transfer.

The real ``BambuFTPClient.upload_file`` over a real local data connection; only the control
channel is a stand-in, so the printer's reply to a transfer we cut short is the test's choice.
A reply of failure (426) or no reply at all used to turn the cancel into "the upload failed":
``upload_file`` returned False, the dispatcher retried it, and the Cancel button did nothing.
"""

import ftplib
import socket
import threading
import time

import pytest

from backend.app.services import bambu_ftp
from backend.app.services.bambu_ftp import BambuFTPClient, with_ftp_retry


class Stop(Exception):
    pass


class Control:
    def __init__(self, reply, port):
        self.reply, self.port = reply, port
        self.sock = self
        self._timeout = 30.0
        self.sent = []

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value

    def transfercmd(self, _command):
        return socket.create_connection(("127.0.0.1", self.port))

    def voidresp(self):
        if self.reply == "silent":
            time.sleep(self._timeout)
            raise TimeoutError("timed out")
        if self.reply == "426":
            raise ftplib.error_temp("426 Failure reading network stream.")
        return "226 Transfer complete."

    def size(self, _path):
        self.sent.append("SIZE")
        return 65536

    def delete(self, path):
        self.sent.append(f"DELE {path}")


@pytest.fixture
def upload(tmp_path):
    server = socket.create_server(("127.0.0.1", 0))

    def sink():
        conn, _ = server.accept()
        with conn:
            while conn.recv(1 << 16):
                pass

    threading.Thread(target=sink, daemon=True).start()
    source = tmp_path / "job.3mf"
    source.write_bytes(b"\0" * (1 << 20))

    def run(reply):
        client = BambuFTPClient("127.0.0.1", "x", timeout=30, printer_model="X1C")
        client._ftp = Control(reply, server.getsockname()[1])

        def stop_at_once(_uploaded, _total):
            raise Stop("cancelled")

        started = time.monotonic()
        with pytest.raises(Stop):
            client.upload_file(source, "/job.3mf", stop_at_once)
        return client._ftp.sent, time.monotonic() - started

    yield run
    server.close()


@pytest.mark.parametrize("reply", ["226", "426"])
def test_a_cancelled_upload_is_cancelled_whatever_the_printer_answers(upload, reply):
    sent, _took = upload(reply)
    # The partial file is removed; its size is never asked -- it says nothing about a cancel.
    assert sent == ["DELE /job.3mf"]


def test_a_printer_that_says_nothing_does_not_hold_the_cancel(upload, monkeypatch):
    monkeypatch.setattr(bambu_ftp, "CANCEL_REPLY_SECONDS", 0.2)
    sent, took = upload("silent")
    assert took < 5, "the wait for the reply to an aborted transfer is bounded, not the upload's minute"
    assert sent == ["DELE /job.3mf"], "the removal is still asked for"


@pytest.mark.asyncio
async def test_a_retry_is_not_started_once_the_work_was_cancelled():
    attempts = []

    async def upload_fails():
        attempts.append(1)
        return False

    assert await with_ftp_retry(upload_fails, max_retries=3, retry_delay=0.01, stop_retrying=lambda: True) is None
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_without_a_cancel_the_retries_run_as_before():
    attempts = []

    async def upload_fails():
        attempts.append(1)
        return False

    assert await with_ftp_retry(upload_fails, max_retries=2, retry_delay=0.01, stop_retrying=lambda: False) is None
    assert len(attempts) == 3


def test_both_dispatch_runners_stop_retrying_and_report_a_cancel_as_a_cancel():
    """The two runners are written out twice; each must hand the retry its cancel and ask it again
    before it calls the upload failed."""
    import inspect

    from backend.app.services.background_dispatch import BackgroundDispatchService as Service

    for runner in (Service._run_reprint_archive, Service._run_print_library_file):
        source = inspect.getsource(runner)
        assert "stop_retrying=lambda: self._is_cancel_requested(job.id)" in source, runner.__name__
        failed = source.index("raise UploadFailed(str(exc) or _upload_failure_message")
        assert "self._raise_if_cancel_requested(job)" in source[failed - 400 : failed], runner.__name__
        not_uploaded = source.index("if not uploaded:\n                    raise UploadFailed")
        assert "self._raise_if_cancel_requested(job)" in source[not_uploaded - 700 : not_uploaded], runner.__name__
