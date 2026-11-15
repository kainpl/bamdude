"""The real FTP client against the mock printer: one session, typed listings (G10 A, M02/M03/M04/M06).

The U0 probes measured these on the pinned code as counterexamples; here they
are assertions on the fixed code. Sessions are counted on the server side.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime

import pytest

from backend.app.services import bambu_ftp
from backend.app.services.bambu_ftp import (
    LISTING_COMPLETE,
    LISTING_MISSING,
    LISTING_UNAVAILABLE,
    LISTING_UNREADABLE,
    _parse_list_mtime,
    download_file_async,
    download_file_bytes_async,
    list_directory_async,
    list_files_checked_async,
)
from backend.app.services.printer_file_lanes import ftp_key, printer_file_runtime
from backend.tests.unit.services import mock_ftp_server as mock

IP, CODE = "127.0.0.1", "12345678"
# How a printer words a CWD it refuses — the same for a missing and a forbidden directory.
REAL_CWD_550 = "Failed to change directory."


class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.opened: list[float] = []


@pytest.fixture
def sessions(monkeypatch):
    counter = Sessions()
    spawn, forget = mock.MockBambuFTPServer._spawn_session, mock.MockBambuFTPServer._forget_session

    def _spawn(self, conn):
        with counter.lock:
            counter.active += 1
            counter.peak = max(counter.peak, counter.active)
            counter.opened.append(time.monotonic())
        return spawn(self, conn)

    def _forget(self, session):
        with counter.lock:
            counter.active -= 1
        return forget(self, session)

    monkeypatch.setattr(mock.MockBambuFTPServer, "_spawn_session", _spawn)
    monkeypatch.setattr(mock.MockBambuFTPServer, "_forget_session", _forget)
    return counter


@pytest.fixture
def slow_retr(monkeypatch):
    """RETR sends its first KiB, then waits for the gate — a healthy but slow transfer."""
    gate, started = threading.Event(), threading.Event()

    def _slow(self, argument):
        local = self._local(argument)
        if local is None or not local.is_file():
            self._respond(550, "Failed to open file.")
            self._close_pasv()
            return
        payload = local.read_bytes()
        conn = self._open_data()
        if conn is None:
            return
        try:
            conn.sendall(payload[:1024])
            started.set()
            gate.wait(10)
            conn.sendall(payload[1024:])
        except OSError:
            self._close_data(conn)
            self._respond(426, "Transfer aborted.")
            return
        self._close_data(conn)
        self._respond(226, "Transfer complete.")

    monkeypatch.setattr(mock._ControlSession, "_cmd_retr", _slow)
    yield gate, started
    gate.set()


async def _until(predicate, seconds=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def _lane_free():
    await _until(lambda: not printer_file_runtime.lane_busy(ftp_key(IP)))


@pytest.mark.asyncio
class TestOneSessionPerPrinter:
    async def test_a_cancelled_read_never_shares_the_printer_with_the_next_session(
        self, patch_ftp_port, sessions, slow_retr
    ):
        server = patch_ftp_port
        server.add_file("timelapse/big.mp4", bytes(range(256)) * 4096)
        gate, started = slow_retr
        read = asyncio.create_task(download_file_bytes_async(IP, CODE, "/timelapse/big.mp4"))
        assert await asyncio.to_thread(started.wait, 10)
        read.cancel()
        await asyncio.gather(read, return_exceptions=True)
        listing = asyncio.create_task(list_directory_async(IP, CODE, "/"))
        await asyncio.sleep(0.5)
        assert sessions.peak == 1  # the listing waits for the cancelled read's thread
        assert not listing.done()
        gate.set()
        assert (await listing).status == LISTING_COMPLETE
        assert sessions.peak == 1
        await _lane_free()

    async def test_the_read_cap_bounds_admission_not_the_transfer(self, patch_ftp_port, sessions, slow_retr):
        """Upstream #3272: a large timelapse over slow Wi-Fi is not failed by a flat cap."""
        server = patch_ftp_port
        payload = bytes(range(256)) * 4096
        server.add_file("timelapse/slow.mp4", payload)
        gate, started = slow_retr
        threading.Timer(1.5, gate.set).start()
        data = await download_file_bytes_async(IP, CODE, "/timelapse/slow.mp4", timeout=0.5)
        assert data == payload
        assert sessions.peak == 1
        await _lane_free()

    async def test_a_download_cancelled_while_queued_never_connects(
        self, patch_ftp_port, sessions, slow_retr, tmp_path
    ):
        server = patch_ftp_port
        server.add_file("timelapse/a.mp4", b"x" * 4096)
        server.add_file("cache/b.3mf", b"y" * 4096)
        gate, started = slow_retr
        holder = asyncio.create_task(download_file_bytes_async(IP, CODE, "/timelapse/a.mp4"))
        assert await asyncio.to_thread(started.wait, 10)
        target = tmp_path / "b.3mf"
        queued = asyncio.create_task(download_file_async(IP, CODE, "/cache/b.3mf", target, timeout=30))
        await asyncio.sleep(0.3)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        cancelled_at = time.monotonic()
        gate.set()
        await holder
        await _lane_free()
        await asyncio.sleep(0.5)
        assert [t for t in sessions.opened if t > cancelled_at] == []
        assert not target.exists()


@pytest.mark.asyncio
class TestTypedListing:
    async def test_a_list_that_fails_after_connect_is_not_an_answer(self, patch_ftp_port):
        server = patch_ftp_port
        server.inject_failure("LIST", 426, "Failure reading network stream.", count=1)
        listing = await list_directory_async(IP, CODE, "/timelapse")
        assert listing.status == LISTING_UNAVAILABLE
        entries, answered = await list_files_checked_async(IP, CODE, "/timelapse")
        assert answered is True  # the failure was injected once; this list is complete
        server.inject_failure("LIST", 426, "Failure reading network stream.", count=1)
        entries, answered = await list_files_checked_async(IP, CODE, "/timelapse")
        assert (entries, answered) == ([], False)

    async def test_a_550_on_a_directory_the_parent_lists_is_unreadable_not_missing(self, patch_ftp_port):
        server = patch_ftp_port
        server.inject_failure("CWD", 550, REAL_CWD_550, count=1)
        listing = await list_directory_async(IP, CODE, "/timelapse", prove_missing=True)
        assert listing.status == LISTING_UNREADABLE

    async def test_absence_is_proven_by_a_complete_parent_listing(self, patch_ftp_port):
        listing = await list_directory_async(IP, CODE, "/record", prove_missing=True)
        assert listing.status == LISTING_MISSING
        nested = await list_directory_async(IP, CODE, "/timelapse/video", prove_missing=True)
        assert nested.status == LISTING_MISSING

    async def test_without_the_proof_a_550_stays_unreadable(self, patch_ftp_port):
        listing = await list_directory_async(IP, CODE, "/record")
        assert listing.status == LISTING_UNREADABLE

    async def test_a_proven_absence_is_an_answer_a_refused_present_directory_is_not(self, patch_ftp_port):
        server = patch_ftp_port
        # /record is not on the card: its parent says so, completely.
        assert await list_files_checked_async(IP, CODE, "/record") == ([], True)
        # /timelapse IS on the card but refuses CWD: no answer at all.
        server.inject_failure("CWD", 550, REAL_CWD_550, count=1)
        assert await list_files_checked_async(IP, CODE, "/timelapse") == ([], False)

    async def test_a_parent_that_cannot_be_listed_proves_nothing(self, patch_ftp_port):
        server = patch_ftp_port
        server.inject_failure("LIST", 426, "Failure reading network stream.", count=-1)
        listing = await list_directory_async(IP, CODE, "/record", prove_missing=True)
        assert listing.status == LISTING_UNREADABLE

    async def test_a_listing_cut_short_is_never_published(self, patch_ftp_port, monkeypatch):
        server = patch_ftp_port
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            server.add_file(f"timelapse/{name}", b"v")

        def _partial(self, argument):
            target = self._listing_target(argument)
            conn = self._open_data()
            if conn is None:
                return
            first = sorted(target.iterdir())[0]
            conn.sendall(f"{self._format_entry(first)}\r\n".encode())
            self._close_data(conn)
            self._respond(426, "Connection closed; transfer aborted.")

        monkeypatch.setattr(mock._ControlSession, "_cmd_list", _partial)
        listing = await list_directory_async(IP, CODE, "/timelapse")
        assert listing.status == LISTING_UNAVAILABLE
        assert listing.entries == ()

    async def test_entries_carry_how_much_of_their_time_is_known(self, patch_ftp_port):
        server = patch_ftp_port
        server.add_file("timelapse/v.mp4", b"v")
        listing = await list_directory_async(IP, CODE, "/timelapse")
        (entry,) = [e for e in listing.entries if e["name"] == "v.mp4"]
        assert entry["mtime_quality"] in {"year_inferred", "date_only"}


@pytest.mark.asyncio
class TestTimelapseListingTrust:
    """U0 counterexample P1-1 as an assertion: a LIST that fails after connect never trusts a baseline."""

    @staticmethod
    def _printer():
        from types import SimpleNamespace

        return SimpleNamespace(id=4242, ip_address=IP, access_code=CODE, model="X1C", name="probe")

    async def test_a_failed_list_leaves_the_card_unanswered(self, patch_ftp_port, monkeypatch):
        from backend.app.services import timelapse_files

        server = patch_ftp_port
        monkeypatch.setattr(timelapse_files, "_supports_internal_timelapse", lambda printer: False)
        server.inject_failure("LIST", 426, "Failure reading network stream.", count=-1)
        videos, _source = await timelapse_files.list_timelapse_videos(self._printer())
        assert videos == []
        assert timelapse_files.last_listing_answered(4242) is False

    async def test_directories_proven_absent_keep_the_card_answered(self, patch_ftp_port, monkeypatch):
        from backend.app.services import timelapse_files

        monkeypatch.setattr(timelapse_files, "_supports_internal_timelapse", lambda printer: False)
        # /timelapse exists and is empty; /timelapse/video, /record and /recording are not on the card.
        videos, _source = await timelapse_files.list_timelapse_videos(self._printer())
        assert videos == []
        assert timelapse_files.last_listing_answered(4242) is True


class TestListYearAgainstUtc:
    def test_an_evening_file_on_a_host_behind_utc_is_not_dated_a_year_back(self):
        # UTC has already moved to Jan 1; the printer wrote the file at 23:30 on Dec 31.
        now = datetime(2027, 1, 1, 0, 10, tzinfo=UTC)
        mtime, quality = _parse_list_mtime("Dec", "31", "23:30", now)
        assert (mtime, quality) == (datetime(2026, 12, 31, 23, 30), "year_inferred")

    def test_a_time_just_ahead_of_utc_stays_this_year(self):
        now = datetime(2027, 3, 10, 12, 0, tzinfo=UTC)
        mtime, _ = _parse_list_mtime("Mar", "10", "18:45", now)  # a printer clock ahead of UTC
        assert mtime == datetime(2027, 3, 10, 18, 45)

    def test_a_time_far_ahead_is_last_year(self):
        now = datetime(2027, 3, 10, 12, 0, tzinfo=UTC)
        mtime, _ = _parse_list_mtime("Nov", "30", "10:15", now)
        assert mtime == datetime(2026, 11, 30, 10, 15)

    def test_a_date_only_entry_keeps_its_year_and_says_so(self):
        now = datetime(2027, 3, 10, tzinfo=UTC)
        assert _parse_list_mtime("Nov", "30", "2024", now) == (datetime(2024, 11, 30), "date_only")

    def test_an_unreadable_date_is_unknown_not_guessed(self):
        now = datetime(2027, 3, 10, tzinfo=UTC)
        assert _parse_list_mtime("Feb", "29", "10:00", now) == (None, "unknown")
        assert _parse_list_mtime("Foo", "1", "10:00", now) == (None, "unknown")

    def test_a_naive_now_is_never_used_as_local_time(self):
        """The reference is UTC whatever the host zone is (upstream 27003de36)."""
        assert bambu_ftp._parse_list_mtime.__defaults__ is None
