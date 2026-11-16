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


# A completed LIST line the parser cannot read as an entry: an .mp4 whose size column
# is not a number (Codex review A3's probe row).
UNREADABLE_ROW = "-rw-r--r-- 1 root root BADSIZE Nov 15 12:00 old.mp4"


@pytest.fixture
def extra_list_lines(monkeypatch):
    """``extra[directory] = lines``: LIST of that directory answers its real entries, then these lines."""
    extra: dict[str, list[str]] = {}
    original = mock._ControlSession._cmd_list

    def _list(self, argument):
        parts = [token for token in argument.split() if not token.startswith("-")]
        directory = "/" + (parts[0] if parts else self._cwd).strip("/")
        target = self._listing_target(argument)
        if directory not in extra or target is None or not target.is_dir():
            return original(self, argument)
        lines = [self._format_entry(child) for child in sorted(target.iterdir(), key=lambda p: p.name)]
        payload = "".join(f"{line}\r\n" for line in [*lines, *extra[directory]]).encode("utf-8", "replace")
        self._send_data(payload)

    monkeypatch.setattr(mock._ControlSession, "_cmd_list", _list)
    return extra


class _FakeListFtp:
    def __init__(self, lines: dict[str, list[str]], refuse: set[str] = frozenset()):
        self.lines = lines
        self.refuse = refuse
        self.where = "/"

    def cwd(self, path):
        if path in self.refuse:
            raise bambu_ftp.ftplib.error_perm("550 Failed to change directory.")
        self.where = path

    def retrlines(self, command, callback):
        for line in self.lines.get(self.where, []):
            callback(line)


def _listing_client(lines, refuse=frozenset()):
    client = bambu_ftp.BambuFTPClient(IP, CODE)
    client._ftp = _FakeListFtp(lines, set(refuse))
    return client


VALID_ROW = "-rw-r--r-- 1 root root 2048 Nov 15 12:00 new.mp4"


class TestAnUnreadableLineIsNoAnswer:
    """Codex review A3: a LIST with a line we cannot read is not an authoritative namespace."""

    def test_a_lone_unreadable_row_is_not_an_empty_directory(self):
        listing = _listing_client({"/timelapse": [UNREADABLE_ROW]}).list_directory("/timelapse")
        assert (listing.status, listing.reason, listing.unparsed) == (LISTING_UNAVAILABLE, "unparsed_entries", 1)
        assert listing.entries == ()

    def test_a_mixed_listing_is_not_complete_and_keeps_what_it_read_for_legacy_callers(self):
        client = _listing_client({"/timelapse": [VALID_ROW, UNREADABLE_ROW]})
        listing = client.list_directory("/timelapse")
        assert listing.status == LISTING_UNAVAILABLE
        assert [e["name"] for e in listing.entries] == ["new.mp4"]
        # The legacy best-effort shape returns what it read, as before G10.
        assert [e["name"] for e in client.list_files("/timelapse")] == ["new.mp4"]

    @pytest.mark.parametrize("noise", ["total 8", "total 0", "", "   "])
    def test_known_noise_is_not_a_lost_entry(self, noise):
        listing = _listing_client({"/timelapse": [noise, VALID_ROW]}).list_directory("/timelapse")
        assert listing.status == LISTING_COMPLETE
        assert [e["name"] for e in listing.entries] == ["new.mp4"]

    def test_a_banner_that_merely_looks_like_noise_is_still_a_lost_entry(self):
        listing = _listing_client({"/timelapse": ["total eight", VALID_ROW]}).list_directory("/timelapse")
        assert listing.status == LISTING_UNAVAILABLE

    def test_a_parent_with_an_unreadable_row_proves_no_absence(self):
        client = _listing_client({"/": [VALID_ROW, UNREADABLE_ROW]}, refuse={"/record"})
        assert client.list_directory_proving("/record").status == LISTING_UNREADABLE

    def test_a_fully_read_parent_still_proves_absence(self):
        client = _listing_client({"/": [VALID_ROW, "total 4"]}, refuse={"/record"})
        assert client.list_directory_proving("/record").status == LISTING_MISSING


@pytest.mark.asyncio
class TestAnUnreadableLineReachesEveryConsumer:
    async def test_the_checked_listing_says_no_answer(self, patch_ftp_port, extra_list_lines):
        server = patch_ftp_port
        server.add_file("timelapse/new.mp4", b"v")
        extra_list_lines["/timelapse"] = [UNREADABLE_ROW]
        entries, answered = await list_files_checked_async(IP, CODE, "/timelapse")
        assert answered is False
        assert [e["name"] for e in entries] == ["new.mp4"]
        # The legacy list keeps its best-effort shape.
        assert [e["name"] for e in await bambu_ftp.list_files_async(IP, CODE, "/timelapse")] == ["new.mp4"]

    async def test_the_coalesced_listing_shows_the_last_complete_one_as_stale(self, patch_ftp_port, extra_list_lines):
        from types import SimpleNamespace

        from backend.app.services.printer_files.ftp import FtpTransport
        from backend.app.services.printer_files.listing import ListingCoalescer

        server = patch_ftp_port
        server.add_file("timelapse/a.mp4", b"v")
        printer = SimpleNamespace(id=7, connector_identity="p-7", ip_address=IP, access_code=CODE, model="X1C")
        coalescer = ListingCoalescer()
        transport = FtpTransport(printer)
        first = await coalescer.list(printer, "external", "/timelapse", "model", transport, wait=10)
        assert (first.status, [f.name for f in first.files]) == (LISTING_COMPLETE, ["a.mp4"])
        extra_list_lines["/timelapse"] = [UNREADABLE_ROW]
        second = await coalescer.list(printer, "external", "/timelapse", "model", transport, wait=10)
        assert (second.status, second.reason, second.stale) == (LISTING_UNAVAILABLE, "unparsed_entries", True)
        assert [f.name for f in second.files] == ["a.mp4"]

    async def test_the_timelapse_baseline_is_not_trusted(self, patch_ftp_port, extra_list_lines, monkeypatch):
        """A readable video beside an unreadable row: the old code returned it as a trusted baseline."""
        from types import SimpleNamespace

        from backend.app.services import timelapse_files

        server = patch_ftp_port
        monkeypatch.setattr(timelapse_files, "_supports_internal_timelapse", lambda printer: False)
        server.add_file("timelapse/new.mp4", b"v")
        extra_list_lines["/timelapse"] = [UNREADABLE_ROW]
        printer = SimpleNamespace(id=4343, ip_address=IP, access_code=CODE, model="X1C", name="probe")
        videos, _source = await timelapse_files.list_timelapse_videos(printer)
        assert videos == []
        assert timelapse_files.last_listing_answered(4343) is False
