import asyncio
import ftplib  # nosec B402
import functools
import logging
import os
import re
import shutil
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from ftplib import FTP, FTP_TLS  # nosec B402
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import TypeVar

from backend.app.services.printer_file_lanes import (
    LaneRefused,
    LaneTimeout,
    LaneWorker,
    WorkerCancelled,
    ftp_key,
    printer_file_runtime,
)
from backend.app.services.printer_files.staging import ReserveSchedule, StagingNotRemoved, discard_staging

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class FtpFailure:
    """Why an FTP operation failed, as the client saw it (upstream 70ee5346).

    ``kind``: ``auth`` / ``timeout`` / ``tls`` / ``network`` (the connect), or
    ``storage`` (553 / 552 — the printer's own storage refused), ``not_found``
    (550), ``rejected`` (any other refusal), ``transfer`` (the connection
    dropped mid-file), ``truncated`` (the printer's copy is the wrong size),
    ``stalled`` (the upload never finished). ``code`` is the reply code where
    the printer gave one.
    """

    kind: str
    detail: str = ""
    code: str | None = None


@dataclass
class UploadReport:
    """The CALLER's record of why its own upload failed.

    Owned by the operation, never kept per printer beside
    ``_last_connect_failure``: a listing or a timelapse fetch running beside a
    dispatch would overwrite a shared record and the dispatch would report the
    wrong cause with full confidence.
    """

    failure: FtpFailure | None = None


def describe_upload_failure(failure: FtpFailure | None) -> str:
    """One sentence for the operator, chosen from what actually went wrong (upstream 70ee5346).

    Every failed upload to a card said "Check if SD card is inserted and
    properly formatted" — after a refused handshake, a rejected access code or
    a timeout too, none of which reaches the card. The card is named only where
    the printer itself refused to store the file; where nothing can say more,
    this says so and points at the log rather than guessing a cause.
    """
    if failure is None:
        return "Could not upload the file to the printer. The server log records what the printer's file service said."
    kind, code = failure.kind, failure.code
    if kind == "storage":
        return (
            f"The printer refused to store the file ({code or 'storage error'}). Check that its SD card is inserted, "
            "has free space and is formatted FAT32 or exFAT."
        )
    if kind == "tls":
        return (
            "The printer's file service answered without TLS — it turned the connection away — so the file was not "
            "sent; its SD card was not involved. Most often another program (a slicer's device page, another "
            "integration) was using the file service; try again once it is closed."
        )
    if kind == "auth":
        return (
            "The printer refused the access code for the file transfer. If the access code changed, update it in the "
            "printer's settings in BamDude."
        )
    if kind == "timeout":
        return (
            "The printer's file service did not answer in time, so the file was not sent. Check that the printer is "
            "on and reachable on the network."
        )
    if kind == "network":
        return (
            "The printer's file service could not be reached, so the file was not sent. Check that the printer is on "
            "and that port 990 is reachable."
        )
    if kind == "stalled":
        return "The upload did not finish in time and was abandoned. Check the network between BamDude and the printer."
    if kind == "transfer":
        return (
            "The connection dropped while the file was being sent, so the upload did not complete. Try again; if it "
            "keeps happening, check the network to the printer."
        )
    if kind == "truncated":
        return (
            "The printer acknowledged the upload, but its copy is the wrong size, so the print was not started. "
            "Try again."
        )
    if kind == "not_found":
        return (
            f"The printer rejected the upload path ({code or '550'}). That is on BamDude's side, not something to "
            "fix on the printer — the server log has the details."
        )
    return f"The printer refused the upload ({code or 'no reply code'}). The server log has the details."


class DeleteResult(Enum):
    """Outcome of an FTP delete attempt.

    Distinguishes "file isn't on the printer" (550, recovery impossible by
    retrying) from "delete failed for some other reason" (network, auth,
    transient FTP error — worth retrying). The post-print SD-cleanup used to
    flatten both into ``False`` and log a WARNING on every successful print
    where the printer self-cleaned its SD card before our cleanup ran — A1 /
    A1 Mini firmware does exactly this (#1721).
    """

    DELETED = "deleted"
    NOT_FOUND = "not_found"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Per-printer FTP serialization.
#
# Bambu printers' FTPS server (vsFTPd) accepts only ~one session at a time. If
# BamDude opens a second / overlapping connection to the same printer — which
# happens on print-complete, where the post-print SD-cleanup and the 3MF
# archive-download both fire at the same IP, plus multi-path retries — the
# printer answers the extra connect in PLAINTEXT (e.g. "421 too many users").
# Because we connect with *implicit* TLS (wrap the socket immediately), the
# client reads that plaintext as a malformed TLS record and raises
# ``[SSL: WRONG_VERSION_NUMBER]``. P2S firmware 01.02.00.00 is especially prone
# to this right after a print finishes (busy flushing the SD card / timelapse).
#
# Serializing every FTP operation per printer IP guarantees we never open a
# second concurrent session, which removes the plaintext-rejection root cause.
# This is separate from the #1401 TLS-1.2 cap (that fixes data-channel 426
# truncation, a different symptom).
# ⚠️ The lane belongs to the ACTUAL session, not to the coroutine (G10 A, spec
# r4 §3.3/§5.1): ``printer_file_lanes`` keeps it locked until the worker thread
# holding the socket has really ended, so a timed-out or cancelled caller can no
# longer leave a live session behind an unlocked lane (U0 probes R1/R2/Q1).


def _ftp_serialized(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Hold the printer's FTP lane for one call; never released under a live worker.

    The wrapped function must take ``ip_address`` as its first positional arg
    (or keyword). Retries (:func:`with_ftp_retry`) still release the lane between
    attempts — but only once the previous attempt's thread has ended.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        ip = kwargs.get("ip_address") if "ip_address" in kwargs else (args[0] if args else None)
        if not ip:
            return await fn(*args, **kwargs)
        async with printer_file_runtime.hold(ftp_key(ip)):
            return await fn(*args, **kwargs)

    return wrapper


async def _on_lane(
    ip_address: str,
    name: str,
    target: Callable[[LaneWorker], T],
    *,
    admission_timeout: float | None = None,
    timeout: float | None = None,
) -> T:
    """Run ``target`` as one owned FTP worker on the printer's lane.

    ``admission_timeout`` bounds the wait for the lane and a worker slot (#2572);
    ``timeout`` bounds the wait for the result. Neither frees the lane early.
    """
    return await printer_file_runtime.call(
        ftp_key(ip_address), name, target, admission_timeout=admission_timeout, timeout=timeout
    )


def _session(worker: LaneWorker, client: "BambuFTPClient") -> bool:
    """Connect ``client`` for ``worker``: the cancel intent is checked before the first byte."""
    worker.checkpoint()
    if not client.connect():
        return False
    abort = getattr(client, "abort", None)
    if abort is not None:
        worker.attach(abort)
    try:
        worker.checkpoint()
    except WorkerCancelled:
        _end_session(worker, client)
        raise
    return True


def _end_session(worker: LaneWorker, client: "BambuFTPClient") -> None:
    abort = getattr(client, "abort", None)
    if abort is not None:
        worker.detach(abort)
    client.disconnect()


def _cancel_caused(worker: LaneWorker, error: BaseException) -> bool:
    """Whether a download's error is what its own cancel did, and may be answered as ``cancelled``.

    A cancel closes the sockets under the transfer, and what ftplib or ssl raise
    then is not a printer's answer. Without a cancel the error is raised again;
    a partial file that could not be removed is raised in every case.
    """
    if not worker.cancel_requested or isinstance(error, StagingNotRemoved):
        return False
    logger.debug("FTP %s ended by its cancel (%s)", worker.name, type(error).__name__)
    return True


#: Bytes per recv for a streamed download (ftplib's default is 8 KiB).
_FETCH_BLOCK = 64 * 1024
#: Longest single wait on a download's data channel before it asks whether it was aborted.
_ABORT_POLL_SECONDS = 0.5

# The outcome of listing one directory (G10 CR2). ``complete`` is the only answer
# that says what the directory holds; ``missing`` is absence PROVEN by a complete
# parent namespace; ``unreadable`` is a 550 (missing OR forbidden); ``unavailable``
# is no session, a failed/partial LIST, or a LIST with a line we could not read as
# an entry (review A3: that line may be a file); ``slow`` is the caller's wait
# running out while the listing may still be running.
LISTING_COMPLETE = "complete"
LISTING_MISSING = "missing"
LISTING_UNREADABLE = "unreadable"
LISTING_UNAVAILABLE = "unavailable"
LISTING_SLOW = "slow"


@dataclass(frozen=True)
class DirectoryListing:
    path: str
    status: str
    entries: tuple[dict, ...] = ()
    reason: str | None = None
    # Lines a finished LIST returned that could not be read as entries. Such a
    # listing is ``unavailable`` (reason ``unparsed_entries``), never complete:
    # its ``entries`` are what WAS read, kept for the legacy best-effort callers,
    # and prove nothing about what the directory holds.
    unparsed: int = 0


def _reply_code(error: BaseException) -> str:
    text = str(error)
    return text[:3] if text[:3].isdigit() else type(error).__name__


_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
    )
}
#: How far ahead of "now" a year-less LIST time may lie before it is read as last year.
#: A day covers a printer clock and a host clock in different zones, and the rollover.
_YEAR_TOLERANCE = timedelta(days=1)


def _parse_list_mtime(month: str, day: str, time_or_year: str, now: datetime) -> tuple[datetime | None, str]:
    """``(wall time, quality)`` of one LIST date; never a guessed instant.

    The printer prints its own wall clock with no zone, so the value stays naive
    and its quality says how much of it is known (G10 A6): ``year_inferred`` (a
    time, the year chosen against ``now`` in UTC with a day of tolerance — that
    picks the year, it does not prove the zone) or ``date_only``; ``unknown``
    when it cannot be read.
    """
    try:
        month_number = _MONTHS[month]
        day_number = int(day)
        if ":" in time_or_year:
            hour, minute = (int(part) for part in time_or_year.split(":", 1))
            reference = now.astimezone(UTC).replace(tzinfo=None)
            candidate = datetime(reference.year, month_number, day_number, hour, minute)
            if candidate > reference + _YEAR_TOLERANCE:
                candidate = candidate.replace(year=reference.year - 1)
            return candidate, "year_inferred"
        return datetime(int(time_or_year), month_number, day_number), "date_only"
    except (KeyError, ValueError):
        return None, "unknown"


#: Lines a LIST may carry that describe no entry, and are therefore not a lost one:
#: a blank line, and the ``total N`` block count some servers print first (vsftpd
#: prints none). Every other line that does not read as an entry may be a file.
_LIST_NOISE = re.compile(r"\s*(?:total\s+\d+\s*)?")


def _is_list_noise(item: str) -> bool:
    return _LIST_NOISE.fullmatch(item) is not None


def _parse_list_line(item: str, directory: str, now: datetime) -> dict | None:
    """One ``ls -l`` line as an entry, or None when it is not one."""
    parts = item.split()
    if len(parts) < 9:
        return None
    name = " ".join(parts[8:])
    is_dir = item.startswith("d")
    try:
        size = int(parts[4]) if not is_dir else 0
    except ValueError:
        return None
    mtime, quality = _parse_list_mtime(parts[5], parts[6], parts[7], now)
    entry = {
        "name": name,
        "is_directory": is_dir,
        "size": size,
        "path": f"{directory.rstrip('/')}/{name}",
        "mtime_quality": quality,
    }
    if mtime:
        entry["mtime"] = mtime
    return entry


class FileNotOnPrinterError(Exception):
    """Raised when a remote FTP path returns 550 (file not found).

    550 means the file does not exist at that path — retrying the same path
    will never succeed. Callers use this sentinel with with_ftp_retry's
    non_retry_exceptions to immediately move on to the next candidate path
    instead of burning the full retry budget (up to 11 × 30s per path) on
    a lookup that cannot recover.
    """


def _graceful_close_data_conn(conn) -> None:
    """Close an FTP data-channel socket, draining the TLS layer first.

    For TLS-wrapped data channels (implicit FTPS) we must ``unwrap()`` the
    SSL layer before closing the underlying TCP socket; otherwise the
    kernel can send a RST in place of FIN while the local send buffer
    still contains the last chunk, causing the server to record a 0-byte
    upload. This matches the behavior of Python's own
    ``ftplib.FTP.storbinary``, which also calls ``conn.unwrap()`` before
    the transfercmd context manager closes the socket.
    """
    try:
        if isinstance(conn, ssl.SSLSocket):
            try:
                conn.unwrap()
            except (OSError, ssl.SSLError):
                # Peer already closed / TLS layer already shut - fall through
                # to the plain close below.
                pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


class ImplicitFTP_TLS(FTP_TLS):
    """FTP_TLS subclass for implicit FTPS (port 990) with model-specific SSL handling.

    X1C/P1S printers (vsFTPd) require SSL with session reuse on the data channel.
    A1/A1 Mini printers have issues with SSL on the data channel entirely and
    timeout waiting for transfer completion. Set skip_session_reuse=True for A1
    printers to skip SSL on the data channel (control channel remains encrypted).
    """

    def __init__(self, *args, skip_session_reuse: bool = False, cap_tls_v1_2: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None
        self.skip_session_reuse = skip_session_reuse
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        # ``create_default_context()`` leaves ``minimum_version`` at
        # MINIMUM_SUPPORTED, so the floor comes from the interpreter's OpenSSL
        # build, not from us — Docker (python:3.13-slim) is floored at TLS 1.2,
        # but bare-metal / appliance venvs could silently negotiate TLS 1.0/1.1.
        # Every Bambu printer speaks TLS 1.2, so declare the floor explicitly.
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        if cap_tls_v1_2:
            # With the floor above this pins the connection to exactly TLS 1.2.
            self.ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2
        # Set by BambuFTPClient.abort from another thread; asked between the waits of retrbinary.
        self.abort_requested = False

    def retrbinary(self, cmd, callback, blocksize=8192, rest=None):
        """``FTP.retrbinary`` whose waits on the data channel are short enough to see an abort.

        ⚠️ On Windows a ``shutdown`` from another thread does not wake a ``recv`` that
        is already waiting on the socket (measured on loopback: 0 of 20, with and
        without a socket timeout; Linux wakes at once), so a cancelled download that
        had stalled kept its lane until the socket timeout. Closing the descriptor
        would wake it, but a worker that was not waiting yet would then read from
        whatever socket takes that handle next. So one wait lasts at most
        ``_ABORT_POLL_SECONDS``; a stall is still no byte for the whole socket
        timeout, as before.

        ⚠️ ``abort_requested`` is asked before every read and again after it, before
        the bytes are handed on — not only when a wait times out (review IA1). A
        cancel can land after the data channel opened but before ``data_conn`` was
        published: the abort then shuts only the control socket, the stream stays
        healthy, no wait ever times out, and the whole file used to arrive anyway.
        """
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            idle = conn.gettimeout()
            conn.settimeout(_ABORT_POLL_SECONDS if idle is None else min(idle, _ABORT_POLL_SECONDS))
            quiet_since = time.monotonic()
            while True:
                if self.abort_requested:
                    raise ConnectionAbortedError("transfer aborted")
                try:
                    data = conn.recv(blocksize)
                except TimeoutError:
                    # A cancel outranks a stall: the loop asks the flag first.
                    if not self.abort_requested and idle is not None and time.monotonic() - quiet_since >= idle:
                        raise
                    continue
                if not data:
                    break
                if self.abort_requested:
                    raise ConnectionAbortedError("transfer aborted")
                quiet_since = time.monotonic()
                callback(data)
            if self.abort_requested:
                # An end of stream our own shutdown caused is not the end of the file.
                raise ConnectionAbortedError("transfer aborted")
            if isinstance(conn, ssl.SSLSocket):
                conn.unwrap()
        return self.voidresp()

    def connect(self, host="", port=990, timeout=-999, source_address=None):
        """Connect to host, wrapping socket in TLS immediately (implicit FTPS)."""
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address:
            self.source_address = source_address

        # Create and wrap socket immediately (implicit TLS)
        self.sock = socket.create_connection((self.host, self.port), self.timeout, source_address=self.source_address)
        self.sock = self.ssl_context.wrap_socket(self.sock, server_hostname=self.host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        """Override to wrap data connection in SSL for X1C/P1S only.

        X1C/P1S printers (vsFTPd) require SSL session reuse on the data channel.
        A1/A1 Mini printers have issues with SSL on the data channel entirely -
        they timeout waiting for the transfer completion response. For A1, we
        skip SSL wrapping on the data channel (control channel remains encrypted).
        """
        conn, size = FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p and not self.skip_session_reuse:
            # X1C/P1S: Wrap data channel with SSL session reuse (required by vsFTPd)
            conn = self.ssl_context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,
            )
        # A1/A1 Mini (skip_session_reuse=True): Don't wrap data channel in SSL
        # The control channel remains encrypted via implicit FTPS
        # Kept so a cancel from another thread can close THIS transfer's socket
        # (BambuFTPClient.abort); ftplib closes it itself when the transfer ends.
        self.data_conn = conn
        return conn, size


#: One budget for the cleartext probe's connect AND read (upstream cc39acfc).
_CLEARTEXT_PROBE_TIMEOUT = 2.0
#: How often one printer is probed at most. Upstream probes once per its FTPS
#: cool-off; we have no cool-off (FTP is serialised per printer instead), so
#: the probe keeps its own window.
_CLEARTEXT_PROBE_WINDOW_SECONDS = 300.0


def _read_cleartext_reply(ip_address: str, port: int) -> str | None:
    """Read what a printer answers the TLS port with, when it is not TLS (audit D5, #2780).

    ``WRONG_VERSION_NUMBER`` means the peer's first bytes were not a TLS record
    — measured upstream, reproducible without a printer: a cleartext ``421``
    banner gives exactly that error, a TLS version mismatch gives
    ``TLSV1_ALERT_PROTOCOL_VERSION`` instead. What it does not say is WHICH
    cleartext message, and that is the part that names the fault; OpenSSL has
    consumed those bytes by the time the error surfaces. So this opens one plain
    connection and reads them.

    ``None`` when the printer said nothing readable — itself informative: a
    healthy implicit-FTPS service sends nothing until it has a ClientHello, so
    silence means the refusal had already passed — or could not be reached.
    """
    sock = None
    # One budget for connect AND read, so the wait this adds to a failed connect
    # is what it says, not double.
    deadline = time.monotonic() + _CLEARTEXT_PROBE_TIMEOUT
    try:
        sock = socket.create_connection((ip_address, port), _CLEARTEXT_PROBE_TIMEOUT)
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        # One read: a refusal is one short line, and this must not become a transfer.
        raw = sock.recv(256)
    except OSError as e:
        logger.debug("Cleartext probe of %s:%s could not read: %s", ip_address, port, e)
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if not raw:
        return None
    # latin-1 cannot fail; control characters go so a stray byte cannot mangle the log line.
    text = raw.decode("latin-1").strip()
    return "".join(c for c in text if c.isprintable()) or None


class BambuFTPClient:
    """FTP client for retrieving files from Bambu Lab printers."""

    FTP_PORT = 990
    # Default timeout in seconds (increased for A1 printers)
    DEFAULT_TIMEOUT = 30
    # Models that may need SSL mode fallback (try prot_p first, fall back to prot_c)
    # These models have varying FTP SSL behavior depending on firmware version
    A1_MODELS = ("A1", "A1 Mini")
    # Chunk size for manual upload transfer (64KB)
    # Smaller chunks provide smoother progress reporting - at typical printer FTP
    # speeds (~50-100KB/s) this gives a progress update roughly every second.
    CHUNK_SIZE = 64 * 1024

    # Cache for working FTP modes per printer IP
    # Maps IP -> "prot_p" or "prot_c"
    _mode_cache: dict[str, str] = {}
    # When each printer was last asked what it answers port 990 with (audit D5).
    _cleartext_probed_at: dict[str, float] = {}
    # Why the last connect to each printer failed, and when: ``(kind, monotonic)``.
    # The 3MF download turns it into the reason an archive is left without its
    # file (audit D6); a successful connect forgets it.
    _last_connect_failure: dict[str, tuple[str, float]] = {}

    def __init__(
        self,
        ip_address: str,
        access_code: str,
        timeout: float | None = None,
        printer_model: str | None = None,
        force_prot_c: bool = False,
    ):
        self.ip_address = ip_address
        self.access_code = access_code
        self.timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        self.printer_model = printer_model
        self.force_prot_c = force_prot_c
        self._ftp: ImplicitFTP_TLS | None = None
        # When the control socket opened, so the close log can say how long the
        # session was held (upstream 10f0900f).
        self._connected_at: float | None = None
        # Why this client's last connect or upload failed (upstream 70ee5346).
        self.failure: FtpFailure | None = None

    def _is_a1_model(self) -> bool:
        """Check if this is an A1 series printer."""
        if not self.printer_model:
            return False
        return self.printer_model in self.A1_MODELS

    def _get_cached_mode(self) -> str | None:
        """Get cached FTP mode for this printer."""
        return self._mode_cache.get(self.ip_address)

    @classmethod
    def cache_mode(cls, ip_address: str, mode: str):
        """Cache the working FTP mode for a printer."""
        cls._mode_cache[ip_address] = mode
        logger.info("FTP mode cached for %s: %s", ip_address, mode)

    def _should_use_prot_c(self) -> bool:
        """Determine if we should use prot_c (clear) mode."""
        # If explicitly forced, use prot_c
        if self.force_prot_c:
            return True
        # Check cache first
        cached = self._get_cached_mode()
        if cached:
            return cached == "prot_c"
        # Default: try prot_p first (will fall back if needed)
        return False

    @classmethod
    def connect_failure_since(cls, ip_address: str, since: float) -> str | None:
        """How the last connect to *ip_address* failed, if it failed at or after *since*.

        ``"auth"`` (the login was refused), ``"timeout"``, ``"tls"`` (the
        handshake failed — a cleartext answer on port 990 among them) or
        ``"network"``; ``None`` when nothing failed since *since* (``time.monotonic``)
        or a connect has succeeded after the failure.
        """
        record = cls._last_connect_failure.get(ip_address)
        if record is None or record[1] < since:
            return None
        return record[0]

    def _note_connect_failure(self, kind: str, detail: str = "") -> None:
        self._last_connect_failure[self.ip_address] = (kind, time.monotonic())
        self.failure = FtpFailure(kind, detail)

    def connect(self) -> bool:
        """Connect to the printer FTP server (implicit FTPS on port 990)."""
        try:
            use_prot_c = self._should_use_prot_c()
            from backend.app.services.ftp_profiles import get_ftp_profile

            profile = get_ftp_profile(self.printer_model)
            logger.debug(
                f"FTP connecting to {self.ip_address}:{self.FTP_PORT} "
                f"(timeout={self.timeout}s, model={self.printer_model}, prot_c={use_prot_c}, "
                f"cap_tls_v1_2={profile.cap_tls_v1_2})"
            )
            self._ftp = ImplicitFTP_TLS(
                skip_session_reuse=use_prot_c,
                cap_tls_v1_2=profile.cap_tls_v1_2,
            )
            self._ftp.connect(self.ip_address, self.FTP_PORT, timeout=self.timeout)
            # Stamped here, not after login: a session that dies during login is
            # the one whose lifetime a reader of the log wants accounted for.
            self._connected_at = time.monotonic()
            logger.debug("FTP connected, logging in as bblp")
            self._ftp.login("bblp", self.access_code)
            if use_prot_c:
                # Use clear (unencrypted) data channel
                logger.debug("FTP logged in, setting prot_c (clear) and passive mode")
                self._ftp.prot_c()
            else:
                # Use protected (encrypted) data channel with session reuse
                logger.debug("FTP logged in, setting prot_p (protected) and passive mode")
                self._ftp.prot_p()
            self._ftp.set_pasv(True)
            # Log welcome message for debugging
            if hasattr(self._ftp, "welcome") and self._ftp.welcome:
                logger.debug("FTP server welcome: %s", self._ftp.welcome)
            logger.info(
                f"FTP connected successfully to {self.ip_address} (model={self.printer_model}, prot_c={use_prot_c})"
            )
            self._last_connect_failure.pop(self.ip_address, None)
            self.failure = None
            return True
        except ftplib.error_perm as e:
            logger.warning("FTP connection permission error to %s: %s", self.ip_address, e)
            self._abandon_connection("login rejected")
            self._note_connect_failure("auth", str(e))
            return False
        except TimeoutError as e:
            logger.warning("FTP connection timed out to %s: %s", self.ip_address, e)
            self._abandon_connection("connect timed out")
            self._note_connect_failure("timeout", str(e))
            return False
        except ssl.SSLError as e:
            logger.warning("FTP SSL error connecting to %s: %s", self.ip_address, e)
            self._note_connect_failure("tls", str(e))
            # Close the dead socket BEFORE asking the printer anything else: the
            # probe below opens a second connection, and the leading theory for
            # this failure is a printer out of connection slots.
            self._abandon_connection("TLS handshake failed")
            if getattr(e, "reason", None) == "WRONG_VERSION_NUMBER":
                self._probe_cleartext_reply()
            return False
        except (OSError, ftplib.Error) as e:
            logger.warning("FTP connection failed to %s: %s (type: %s)", self.ip_address, e, type(e).__name__)
            self._abandon_connection("connect failed")
            self._note_connect_failure("network", str(e))
            return False

    def _probe_cleartext_reply(self) -> None:
        """Log what the printer answered port 990 with — at most once per window (audit D5).

        Only on ``WRONG_VERSION_NUMBER``: a protocol-version alert means the peer
        did speak TLS, so there is nothing in the clear to read. The printer's
        own words turn a guess ("the file service wedged") into a cause — an FTP
        refusal such as "421 Too many connections" would settle it.
        """
        now = time.monotonic()
        last = self._cleartext_probed_at.get(self.ip_address)
        if last is not None and now - last < _CLEARTEXT_PROBE_WINDOW_SECONDS:
            return
        self._cleartext_probed_at[self.ip_address] = now
        reply = _read_cleartext_reply(self.ip_address, self.FTP_PORT)
        if reply:
            logger.warning(
                "Printer %s answered port %s in cleartext with: %s — that is what the TLS handshake read as a "
                "malformed record. Please include this line if you report it.",
                self.ip_address,
                self.FTP_PORT,
                reply,
            )
        else:
            logger.warning(
                "Printer %s sent nothing readable in cleartext on port %s, so its file service was speaking TLS "
                "again by the time we asked — the refusal was momentary.",
                self.ip_address,
                self.FTP_PORT,
            )

    def _held_for(self) -> str:
        """How long the control socket has been open, for the close log."""
        if self._connected_at is None:
            return "unknown"
        return f"{time.monotonic() - self._connected_at:.1f}s"

    def _abandon_connection(self, reason: str) -> None:
        """Drop a connection that never became usable, closing its socket (upstream 10f0900f).

        Every failure path used to clear ``self._ftp`` and leave the socket to the
        garbage collector — a half-open session held against a printer that
        serves about one at a time. One DEBUG line says how it closed, so every
        connect in a debug log has a matching close.
        """
        ftp = self._ftp
        self._ftp = None
        held = self._held_for()
        self._connected_at = None
        if ftp is None:
            return
        try:
            ftp.close()
        except (OSError, ftplib.Error, EOFError):
            pass  # Best-effort; the socket may already be gone
        logger.debug("FTP session to %s closed without QUIT (%s), held %s", self.ip_address, reason, held)

    def abort(self) -> None:
        """Close this session's sockets from another thread, so a blocked recv/send returns.

        The owner of the lane still waits for the worker thread to end; this only
        makes that end come now instead of at the next socket timeout.

        ⚠️ The plain socket's ``shutdown``, never ``SSLSocket.shutdown``: the SSL
        one also drops the socket's TLS object under the worker, whose blocked
        read then returns EOF and whose ``unwrap`` raises ``ValueError("No SSL
        wrapper around …")`` — an error no transfer handler expected (review A2,
        captured on loopback FTPS). Shutting the descriptor alone ends the read
        the same way and leaves the worker an ordinary socket error.

        ⚠️ The flag comes first: on Windows the shutdown wakes no ``recv`` that is
        already waiting, and the download's own short waits
        (:meth:`ImplicitFTP_TLS.retrbinary`) are what end it there.
        """
        ftp = self._ftp
        if ftp is None:
            return
        ftp.abort_requested = True
        for sock in (getattr(ftp, "data_conn", None), getattr(ftp, "sock", None)):
            if sock is None:
                continue
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass  # already closed, or never connected

    def disconnect(self):
        """Disconnect from the FTP server."""
        if self._ftp:
            held = self._held_for()
            try:
                self._ftp.quit()
            except (OSError, ftplib.Error, EOFError) as e:
                # ``quit()`` sends QUIT and only then closes; when the send
                # raises, ftplib never reaches its own close and the socket stays
                # open. Close it here rather than leaving it to the GC.
                self._abandon_connection(f"QUIT failed: {e}")
            else:
                self._ftp = None
                self._connected_at = None
                logger.debug("FTP session to %s closed (QUIT acknowledged), held %s", self.ip_address, held)

    def list_files(self, path: str = "/") -> list[dict]:
        """List a directory: the entries a finished LIST could read, else ``[]`` (legacy shape).

        ⚠️ Best effort, as before G10: ``[]`` is "empty" and "could not tell" at
        once, and a LIST with an unreadable line still returns the lines it read.
        :meth:`list_directory` says which.
        """
        return list(self.list_directory(path).entries)

    def list_directory(self, path: str = "/", *, now: datetime | None = None) -> "DirectoryListing":
        """List ``path`` and say what the answer proves (G10 CR2).

        The outcome is decided inside CWD/LIST, never by the connect: a 550 on
        CWD or LIST means missing OR forbidden (``unreadable`` — the printers
        word both the same), any other failure or a LIST cut short means
        ``unavailable``, and a partial listing is never published.

        ⚠️ A finished LIST is ``complete`` only when every line it returned is an
        entry or known noise (:data:`_LIST_NOISE`). A line we cannot read may be
        a file, so such a listing is ``unavailable`` with reason
        ``unparsed_entries`` (review A3): an empty-looking directory must never
        hide a video the baseline would later take for new.
        """
        if not self._ftp:
            return DirectoryListing(path, LISTING_UNAVAILABLE, reason="not_connected")
        try:
            self._ftp.cwd(path)
        except ftplib.error_perm as e:
            return DirectoryListing(path, LISTING_UNREADABLE, reason=_reply_code(e))
        except (OSError, EOFError, ftplib.Error) as e:
            logger.info("FTP CWD %s failed: %s", path, e)
            return DirectoryListing(path, LISTING_UNAVAILABLE, reason=type(e).__name__)
        items: list[str] = []
        try:
            self._ftp.retrlines("LIST", items.append)
        except ftplib.error_perm as e:
            return DirectoryListing(path, LISTING_UNREADABLE, reason=_reply_code(e))
        except (OSError, EOFError, ftplib.Error) as e:
            logger.info("FTP list_files failed for %s: %s", path, e)
            return DirectoryListing(path, LISTING_UNAVAILABLE, reason=type(e).__name__)
        reference = now or datetime.now(UTC)
        entries: list[dict] = []
        unparsed = 0
        for item in items:
            if _is_list_noise(item):
                continue
            entry = _parse_list_line(item, path, reference)
            if entry is None:
                unparsed += 1
            else:
                entries.append(entry)
        if unparsed:
            logger.info(
                "FTP LIST of %s returned %d line(s) that are not entries — not a complete listing", path, unparsed
            )
            return DirectoryListing(
                path, LISTING_UNAVAILABLE, tuple(entries), reason="unparsed_entries", unparsed=unparsed
            )
        logger.debug("Listed %s files in %s", len(entries), path)
        return DirectoryListing(path, LISTING_COMPLETE, tuple(entries))

    def list_directory_proving(self, path: str, *, max_ancestors: int = 3) -> "DirectoryListing":
        """:meth:`list_directory`, and turn an ambiguous 550 into a PROVEN absence where possible.

        A 550 cannot tell missing from forbidden, so absence is proven only by a
        complete, fully parsed listing of the parent that lacks the exact child
        name — on this same session (U0 acceptance §3.2). A parent that is
        itself unreadable is proven absent the same way, up to ``max_ancestors``
        levels. A child the parent does list, or any ambiguity, stays
        ``unreadable``.
        """
        listing = self.list_directory(path)
        if listing.status != LISTING_UNREADABLE:
            return listing
        chain = PurePosixPath(path if path.startswith("/") else "/" + path)
        child = chain
        for _ in range(max_ancestors):
            parent = child.parent
            if parent == child:
                break
            parent_listing = self.list_directory(str(parent))
            if parent_listing.status == LISTING_COMPLETE:
                # Complete means fully parsed: a parent with an unreadable line is
                # ``unavailable`` and proves nothing (the branch below).
                names = {entry["name"] for entry in parent_listing.entries}
                if child.name in names:
                    return listing  # present but refused: unreadable, not missing
                return DirectoryListing(path, LISTING_MISSING, reason=f"absent from {parent}")
            if parent_listing.status != LISTING_UNREADABLE:
                return listing
            child = parent
        return listing

    def download_file(self, remote_path: str) -> bytes | None:
        """Download a file from the printer."""
        if not self._ftp:
            return None

        try:
            buffer = BytesIO()
            self._ftp.retrbinary(f"RETR {remote_path}", buffer.write)
        except (OSError, ftplib.Error):
            return None
        data = buffer.getvalue()
        # A short read is a failed read, here as on the file path (G10 A5): the
        # bytes path served routes, timelapse reads and cleanup hashing with no
        # check at all. No SIZE answer is no evidence and changes nothing.
        server_size = self.get_file_size(remote_path)
        if server_size is not None and server_size >= 0 and len(data) != server_size:
            logger.warning(
                "FTP read of %s is short: got %s bytes, the printer reports %s — treating as failed",
                remote_path,
                len(data),
                server_size,
            )
            return None
        return data

    def fetch_to_file(
        self,
        remote_path: str,
        local_path: Path,
        *,
        byte_budget: int | None = None,
        reserve_bytes: int = 0,
        progress: Callable[[int], None] | None = None,
    ) -> tuple[str, int, str | None, str | None]:
        """Stream ``remote_path`` to ``local_path``: ``(status, received, check, reason)``.

        The G10 download-to-file contract on one session (see printer_files.base
        TRANSFER_*). One chunk at a time reaches the disk; a budget, a disk
        reserve or a cancel stops the transfer.

        ⚠️ Every way out but a completed, checked file removes the file this call
        created (review A2) — including an error nobody listed, which a cancel's
        socket abort can raise from deep inside ftplib or ssl; that error is
        raised again once the file is gone. A file that cannot be removed raises
        :class:`StagingNotRemoved` instead of any outcome. The volume's reserve
        is checked before the first write, every 8 MiB and for the unchecked
        tail (:class:`ReserveSchedule`, review A5).
        """
        if not self._ftp:
            return "unavailable", 0, None, "not_connected"
        received = 0
        created = False
        result: tuple[str, int, str | None, str | None] | None = None
        reserve = ReserveSchedule(reserve_bytes)
        volume = local_path.parent

        class _Stop(Exception):
            def __init__(self, status: str, reason: str):
                super().__init__(reason)
                self.status = status
                self.reason = reason

        def transfer() -> tuple[str, int, str | None, str | None]:
            nonlocal received, created
            volume.mkdir(parents=True, exist_ok=True)
            with open(local_path, "wb") as handle:
                created = True

                def write(chunk: bytes) -> None:
                    nonlocal received
                    received += len(chunk)
                    if byte_budget is not None and received > byte_budget:
                        raise _Stop("limit_exceeded", "byte_budget")
                    if reserve.due_before(received) and not reserve.fits(shutil.disk_usage(volume).free, len(chunk)):
                        raise _Stop("insufficient_storage", "reserve")
                    handle.write(chunk)
                    if progress is not None:
                        progress(received)

                self._ftp.retrbinary(f"RETR {remote_path}", write, blocksize=_FETCH_BLOCK)
                handle.flush()
                os.fsync(handle.fileno())
            if reserve.due_final(received) and not reserve.fits(shutil.disk_usage(volume).free):
                raise _Stop("insufficient_storage", "reserve")
            server_size = self.get_file_size(remote_path)
            if server_size is not None and server_size >= 0:
                if received != server_size:
                    return "short_read", received, None, f"{received}/{server_size}"
                return "completed", received, "size_verified", None
            return "completed", received, "size_unknown", None

        try:
            result = transfer()
        except _Stop as stop:
            result = stop.status, received, None, stop.reason
        except ftplib.error_perm as e:
            code = _reply_code(e)
            result = ("not_readable" if code == "550" else "protocol_failed"), received, None, code
        except TimeoutError:
            result = "stalled", received, None, "socket_timeout"
        except (OSError, EOFError, ftplib.Error) as e:
            result = "unavailable", received, None, type(e).__name__
        finally:
            # ``result`` is None while an unexpected error propagates: discard first.
            if created and (result is None or result[0] != "completed"):
                discard_staging(local_path)
        return result

    def download_to_file(self, remote_path: str, local_path: Path) -> bool:
        """Download a file from the printer to local filesystem.

        ⚠️ Anything but a successful download removes the file this call created
        (review A2): a refusal, a short read, a dead socket and an unexpected
        error alike, which is then raised again. A 550 still raises
        :class:`FileNotOnPrinterError`, after the empty file ``open`` made is gone.
        """
        if not self._ftp:
            logger.warning("download_to_file called but FTP not connected")
            return False

        created = False
        downloaded = False
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with open(local_path, "wb") as f:
                created = True
                self._ftp.retrbinary(f"RETR {remote_path}", f.write)
                f.flush()
                os.fsync(f.fileno())
            file_size = local_path.stat().st_size if local_path.exists() else 0
            if file_size == 0:
                logger.warning("FTP download returned 0 bytes for %s", remote_path)
                return False
            # A short read is a failed download, not a file (upstream 55cc64c8):
            # a truncated 3MF attached to an archive fails far from the cause.
            # Asked AFTER the transfer, in the same session — one command, and
            # the multi-path walk pays nothing for the candidates that 550. No
            # answer (firmware without SIZE) is no evidence and changes nothing.
            server_size = self.get_file_size(remote_path)
            if server_size is not None and server_size >= 0 and file_size != server_size:
                logger.warning(
                    "FTP download of %s is short: got %s bytes, the printer reports %s — treating as failed",
                    remote_path,
                    file_size,
                    server_size,
                )
                return False
            logger.info("Successfully downloaded %s to %s (%s bytes)", remote_path, local_path, file_size)
            downloaded = True
            return True
        except (OSError, ftplib.Error) as e:
            # 550 means the file is not at this path. Surface as a sentinel so
            # callers can abandon this path immediately and advance to the next
            # candidate instead of retrying 11× at 30s intervals (the pattern
            # that cost #972's reporter ~48 min of dead retries).
            if isinstance(e, ftplib.error_perm) and str(e).startswith("550"):
                logger.info("FTP download failed for %s: %s (not on printer)", remote_path, e)
                raise FileNotOnPrinterError(f"{remote_path}: {e}") from e
            # Log at INFO level so we can see failures in normal logs
            logger.info("FTP download failed for %s: %s", remote_path, e)
            return False
        finally:
            if created and not downloaded:
                discard_staging(local_path)

    def diagnose_storage(self) -> dict:
        """Run storage diagnostics and return results. For debugging upload issues."""
        results = {
            "connected": self._ftp is not None,
            "can_list_root": False,
            "root_files": [],
            "can_list_cache": False,
            "storage_info": None,
            "pwd": None,
            "errors": [],
        }

        if not self._ftp:
            results["errors"].append("FTP not connected")
            return results

        # Try to get current directory
        try:
            results["pwd"] = self._ftp.pwd()
            logger.debug("FTP current directory: %s", results["pwd"])
        except (OSError, ftplib.Error) as e:
            results["errors"].append(f"PWD failed: {e}")
            logger.debug("FTP PWD failed: %s", e)

        # Try to list root directory
        try:
            self._ftp.cwd("/")
            items = []
            self._ftp.retrlines("LIST", items.append)
            results["can_list_root"] = True
            results["root_files"] = items[:10]  # First 10 entries
            logger.debug("FTP root listing (%s items): %s", len(items), items[:5])
        except (OSError, ftplib.Error) as e:
            results["errors"].append(f"LIST / failed: {e}")
            logger.debug("FTP LIST / failed: %s", e)

        # Try to list /cache (should exist on all printers)
        try:
            self._ftp.cwd("/cache")
            items = []
            self._ftp.retrlines("LIST", items.append)
            results["can_list_cache"] = True
            logger.debug("FTP /cache listing: %s items", len(items))
        except (OSError, ftplib.Error) as e:
            results["errors"].append(f"LIST /cache failed: {e}")
            logger.debug("FTP LIST /cache failed: %s", e)

        # Try to get storage info
        try:
            results["storage_info"] = self.get_storage_info()
            logger.debug("FTP storage info: %s", results["storage_info"])
        except (OSError, ftplib.Error) as e:
            results["errors"].append(f"Storage info failed: {e}")

        return results

    def _uploaded_size_verdict(self, remote_path: str, expected: int) -> tuple[str, int | None]:
        """Ask the printer how big the file it just took actually is.

        Returns the verdict and the size behind it: ``"ok"``, ``"truncated"``,
        or ``"unknown"`` — three answers, not two, because a printer whose FTP
        has no ``SIZE`` must not be read as a failed upload. ``unknown`` leaves
        the caller exactly where it was before the probe existed.

        The size comes back with the verdict so no caller has to ask twice; one
        of them wants it for its log line, and a second round-trip to this
        daemon is exactly what this method is careful about.

        ⚠️ Runs on the session that did the upload. A fresh connection for the
        check would be serialised behind ``@_ftp_serialized`` per IP and is how
        the printer's FTP daemon was wedged during the tunnel investigation —
        one extra command in an open session is the whole budget here.

        ⚠️ Size, not a hash. The printer's FTP exposes no digest of what it
        stored, so bit-rot is out of reach; a transfer that stopped early is
        not, and that is the failure this codebase has actually had.
        """
        server_size = self.get_file_size(remote_path)
        if server_size is None:
            return "unknown", None
        return ("ok" if server_size == expected else "truncated"), server_size

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Upload a file to the printer with optional progress callback.

        On failure ``self.failure`` says why (upstream 70ee5346).
        """
        self.failure = None
        if not self._ftp:
            logger.warning("upload_file: FTP not connected")
            self.failure = FtpFailure("network", "not connected")
            return False

        try:
            file_size = local_path.stat().st_size if local_path.exists() else 0
            logger.info("FTP uploading %s (%s bytes) to %s", local_path, file_size, remote_path)

            uploaded = 0
            callback_exception: Exception | None = None

            # Use manual transfer instead of storbinary() for A1 compatibility
            # A1 printers have issues with storbinary's voidresp() hanging after transfer
            with open(local_path, "rb") as f:
                logger.debug("FTP STOR command starting for %s", remote_path)
                t0 = time.monotonic()
                conn = self._ftp.transfercmd(f"STOR {remote_path}")
                logger.info(
                    "FTP data channel ready in %.1fs (PASV + TLS handshake)",
                    time.monotonic() - t0,
                )

                # Set explicit socket options for reliable transfer
                conn.setblocking(True)
                conn.settimeout(self.timeout)

                try:
                    while True:
                        chunk = f.read(self.CHUNK_SIZE)
                        if not chunk:
                            logger.debug("FTP upload: final chunk reached")
                            break

                        conn.sendall(chunk)
                        uploaded += len(chunk)
                        logger.debug("FTP upload progress: %s/%s bytes", uploaded, file_size)

                        if progress_callback:
                            try:
                                progress_callback(uploaded, file_size)
                            except Exception as e:
                                callback_exception = e
                                logger.info(
                                    "FTP upload callback requested stop for %s at %s/%s bytes: %s",
                                    remote_path,
                                    uploaded,
                                    file_size,
                                    e,
                                )
                                break

                except OSError as e:
                    logger.error("FTP connection lost during upload: %s", e)
                    raise
                finally:
                    _graceful_close_data_conn(conn)

            # Wait for the server's 226 "Transfer complete" response to confirm
            # the file has been flushed to the SD card. Without this, the printer
            # may try to read an incomplete file when the print command is sent,
            # causing 0500-C010 "MicroSD Card read/write exception" errors.
            # See: https://bugs.python.org/issue25458 (ftplib response desync)
            try:
                old_timeout = self._ftp.sock.gettimeout()
                # Use a generous timeout - H2D printers can take 30+ seconds
                # to send the 226 after the data channel closes.
                self._ftp.sock.settimeout(max(self.timeout, 60))
                try:
                    resp = self._ftp.voidresp()
                    logger.info("FTP STOR confirmed for %s: %s", remote_path, resp.strip())
                finally:
                    self._ftp.sock.settimeout(old_timeout)
            except ftplib.Error as e:
                # The printer's FTP server explicitly told us the transfer
                # failed (e.g. 426 "Failure reading network stream" seen on
                # some P2S firmware revisions). Two interpretations exist:
                #   (a) The file is truncated on the SD card and we MUST NOT
                #       send a print command for it (upstream #1417 — silent
                #       0500_C010 / "unable to parse 3mf file" 30s into the
                #       print attempt).
                #   (b) The 426 is noise from the TLS data-channel close
                #       racing the 226 confirmation; the file is fully on
                #       the SD card (upstream #1417-followup — same hardware
                #       worked on v0.2.4.1 after a clean SD-card check).
                # Disambiguate with a server-side SIZE probe. Match → noise,
                # mismatch / probe-fails → real truncation. Upstream Bambuddy
                # #1417 / commit 1fac0276 + #1417-followup / commit 9c934c90.
                verdict, server_size = self._uploaded_size_verdict(remote_path, file_size)
                if verdict == "ok":
                    # INFO, not WARNING: a 426 whose bytes verify is the normal
                    # way Bambu FTPS ends a transfer, not a fault. Upstream #2987
                    # counted 54 in one support bundle, each followed by a
                    # completed upload, burying the handshake failures that
                    # actually cost prints. The unverified branch stays an error.
                    logger.info(
                        "FTP STOR returned %s for %s but SIZE confirms %d bytes "
                        "on disk — treating as transient %s (file intact)",
                        e,
                        remote_path,
                        server_size,
                        type(e).__name__,
                    )
                else:
                    logger.error(
                        "FTP STOR rejected by printer for %s: %s (%s); "
                        "SIZE=%s, expected %d — file is truncated, not sending print command",
                        remote_path,
                        e,
                        type(e).__name__,
                        server_size,
                        file_size,
                    )
                    self.failure = FtpFailure("truncated", str(e))
                    return False
            except Exception as e:
                # Timeout or socket-level error reading 226 — the data was sent
                # on our side and the printer may still have written the file.
                # H2D can take 30+ seconds to send 226 after the data channel
                # closes, so we proceed with a warning rather than failing here.
                # ⚠️ This branch used to proceed on hope alone: "the data was
                # sent on our side and the printer may still have written the
                # file". The probe answers exactly that, and this is the path
                # where being wrong is worst — no 226 means nothing confirmed
                # the write, so a truncated file here reaches start_print and
                # fails a plate in.
                verdict, _ = self._uploaded_size_verdict(remote_path, file_size)
                if verdict == "truncated":
                    logger.error(
                        "FTP STOR unconfirmed for %s (%s) AND the printer's copy is the wrong size "
                        "(expected %d) — not sending a print command for it",
                        remote_path,
                        type(e).__name__,
                        file_size,
                    )
                    self.failure = FtpFailure("truncated", f"no 226: {type(e).__name__}")
                    return False
                logger.warning(
                    "FTP STOR confirmation not received for %s (proceeding, size %s): %s (%s)",
                    remote_path,
                    verdict,
                    e,
                    type(e).__name__,
                )

            if callback_exception is not None:
                cleanup_result: DeleteResult = DeleteResult.FAILED
                try:
                    cleanup_result = self.delete_file(remote_path)
                except Exception as cleanup_error:
                    logger.warning("FTP cancel cleanup failed for %s: %s", remote_path, cleanup_error)

                # NOT_FOUND is success here — the partial file is gone (printer
                # may have already swept on cancel), which is the goal.
                if cleanup_result in (DeleteResult.DELETED, DeleteResult.NOT_FOUND):
                    logger.info("FTP cancel cleanup succeeded for %s (%s)", remote_path, cleanup_result.value)
                    raise callback_exception

                raise RuntimeError(
                    f"Upload cancelled but failed to remove partial file {remote_path} from printer"
                ) from callback_exception

            # ⚠️ A 226 says the printer finished reading the stream, not that
            # what it stored is what we sent. Nothing verified the outgoing file
            # until here — while the two paths where a file ARRIVES both got a
            # ZIP check after uvloop silently truncated 3MFs (see
            # inv-vp-ftp-asyncio-loop). This is the same class of failure on the
            # way out, and it costs one command in a session already open.
            if self._uploaded_size_verdict(remote_path, file_size)[0] == "truncated":
                logger.error(
                    "FTP upload of %s was acknowledged but the printer's copy is the wrong size "
                    "(expected %d) — not sending a print command for it",
                    remote_path,
                    file_size,
                )
                self.failure = FtpFailure("truncated", "size check after 226")
                return False

            elapsed = time.monotonic() - t0
            speed_kbs = (file_size / 1024) / elapsed if elapsed > 0 else 0
            logger.info(
                "FTP upload complete: %s (%s bytes in %.1fs, %.0f KB/s)",
                remote_path,
                file_size,
                elapsed,
                speed_kbs,
            )
            return True
        except ftplib.error_perm as e:
            # Permanent FTP error (4xx/5xx response)
            error_code = str(e)[:3] if str(e) else "unknown"
            logger.error("FTP upload failed for %s: %s (error code: %s)", remote_path, e, error_code)
            if error_code == "553":
                logger.error(
                    "FTP 553 error - Could not create file. Possible causes: "
                    "1) No SD card inserted, 2) SD card full, 3) SD card not formatted correctly (needs FAT32/exFAT), "
                    "4) Printer busy/not ready, 5) File path issue"
                )
            elif error_code == "550":
                logger.error("FTP 550 error - File/directory not found or permission denied")
            elif error_code == "552":
                logger.error("FTP 552 error - Storage quota exceeded (SD card full?)")
            kind = "storage" if error_code in ("553", "552") else "not_found" if error_code == "550" else "rejected"
            self.failure = FtpFailure(kind, str(e), error_code)
            return False
        except (OSError, ftplib.Error) as e:
            logger.error("FTP upload failed for %s: %s (type: %s)", remote_path, e, type(e).__name__)
            self.failure = FtpFailure("transfer", f"{type(e).__name__}: {e}")
            return False

    def upload_bytes(self, data: bytes, remote_path: str) -> bool:
        """Upload bytes to the printer."""
        if not self._ftp:
            return False

        try:
            # Use manual transfer instead of storbinary() for A1 compatibility
            conn = self._ftp.transfercmd(f"STOR {remote_path}")
            conn.setblocking(True)
            conn.settimeout(self.timeout)

            try:
                # Send data in chunks
                offset = 0
                while offset < len(data):
                    chunk = data[offset : offset + self.CHUNK_SIZE]
                    conn.sendall(chunk)
                    offset += len(chunk)
            except OSError as e:
                logger.error("FTP connection lost during upload_bytes: %s", e)
                raise
            finally:
                _graceful_close_data_conn(conn)
            # Wait for 226 confirmation (see upload_file for rationale)
            try:
                old_timeout = self._ftp.sock.gettimeout()
                self._ftp.sock.settimeout(max(self.timeout, 60))
                try:
                    self._ftp.voidresp()
                finally:
                    self._ftp.sock.settimeout(old_timeout)
            except ftplib.Error as e:
                # Server-explicit transfer rejection (e.g. 426) — disambiguate
                # via SIZE check against the buffer length we sent. Match →
                # noise (TLS data-channel close raced 226); mismatch / probe-
                # fails → real truncation. Upstream Bambuddy #1417 / commit
                # 1fac0276 + #1417-followup / 9c934c90.
                server_size = self.get_file_size(remote_path)
                expected = len(data)
                if server_size is not None and server_size == expected:
                    # INFO for the same reason as upload_file (upstream #2987).
                    logger.info(
                        "FTP STOR returned %s for %s but SIZE confirms %d bytes "
                        "on disk — treating as transient %s (file intact)",
                        e,
                        remote_path,
                        server_size,
                        type(e).__name__,
                    )
                else:
                    logger.error(
                        "FTP STOR rejected by printer for %s: %s (%s); SIZE=%s, expected %d — file is truncated",
                        remote_path,
                        e,
                        type(e).__name__,
                        server_size,
                        expected,
                    )
                    return False
            except Exception:
                pass  # Timeout / socket-level — proceed, data was sent.
            return True
        except (OSError, ftplib.Error):
            return False

    def delete_file(self, remote_path: str) -> DeleteResult:
        """Delete a file from the printer.

        Returns :class:`DeleteResult` distinguishing the file-not-found case
        (550) from network / auth / transient FTP failure. Callers that just
        want "did it work" should check ``result == DeleteResult.DELETED``.
        """
        if not self._ftp:
            return DeleteResult.FAILED

        try:
            self._ftp.delete(remote_path)
            return DeleteResult.DELETED
        except ftplib.error_perm as e:
            if str(e).startswith("550"):
                logger.debug("FTP delete: %s not on printer (550)", remote_path)
                return DeleteResult.NOT_FOUND
            logger.warning("Failed to delete %s: %s", remote_path, e)
            return DeleteResult.FAILED
        except (OSError, ftplib.Error) as e:
            logger.warning("Failed to delete %s: %s", remote_path, e)
            return DeleteResult.FAILED

    def remove_dir(self, remote_path: str) -> bool:
        """Remove an (already-emptied) directory from the printer."""
        if not self._ftp:
            return False
        try:
            self._ftp.rmd(remote_path)
            return True
        except (OSError, ftplib.Error) as e:
            logger.warning("Failed to remove dir %s: %s", remote_path, e)
            return False

    def clear_all(self, root: str = "/", may_run: Callable[[], bool] | None = None) -> dict:
        """Clear the SD card: delete every file, and remove nested folders.

        The top-level folders that live directly in ``root`` (e.g. ``/cache``,
        ``/model``, ``/timelapse``) are KEPT — only their contents are wiped,
        since the printer expects those standard directories to exist. Any
        folder nested deeper than that is removed outright once emptied. Files
        are deleted at every level, including the root.

        Reuses this single connection for the whole walk — opening a fresh FTP
        session per entry would be punishingly slow on an SD card with hundreds
        of cached models. Returns ``{"deleted", "failed", "folders_removed",
        "stopped"}``.

        ``may_run`` is asked before every deletion (G10 B): once it says no —
        a print started or became expected — nothing further is removed and
        ``stopped`` is True. What was already deleted stays deleted.
        """
        if not self._ftp:
            return {"deleted": 0, "failed": 0, "folders_removed": 0, "stopped": False}

        stats = {"deleted": 0, "failed": 0, "folders_removed": 0, "stopped": False}

        def _allowed() -> bool:
            if stats["stopped"]:
                return False
            if may_run is not None and not may_run():
                stats["stopped"] = True
                return False
            return True

        def _clean(dir_path: str, depth: int) -> None:
            # depth: 0 == root, 1 == top-level folder, 2+ == nested folder.
            for entry in self.list_files(dir_path):
                if not _allowed():
                    return
                if entry["is_directory"]:
                    # Defensive: some servers surface "." / ".." in LIST.
                    if entry["name"] in (".", ".."):
                        continue
                    child = entry["path"]
                    child_depth = depth + 1
                    _clean(child, child_depth)
                    # Remove the folder itself only when nested (depth >= 2).
                    # Top-level folders (depth 1) and the root are preserved.
                    if child_depth >= 2 and _allowed():
                        if self.remove_dir(child):
                            stats["folders_removed"] += 1
                        else:
                            stats["failed"] += 1
                elif self.delete_file(entry["path"]) == DeleteResult.DELETED:
                    stats["deleted"] += 1
                else:
                    stats["failed"] += 1

        # Folder nesting on an SD card is shallow, so plain recursion is safe
        # and keeps the empty-bottom-up removal order trivial.
        _clean(root, 0)
        logger.info(
            "clear_all under %s: deleted=%s folders_removed=%s failed=%s",
            root,
            stats["deleted"],
            stats["folders_removed"],
            stats["failed"],
        )
        return stats

    def rename_file(self, from_path: str, to_path: str) -> bool | None:
        """Rename/move a file on the printer. Returns True on success, None if not found, False on error."""
        if not self._ftp:
            return False
        try:
            self._ftp.rename(from_path, to_path)
            return True
        except ftplib.error_perm as e:
            if "550" in str(e):
                logger.debug("Rename skipped (not found) %s: %s", from_path, e)
                return None
            logger.warning("Failed to rename %s -> %s: %s", from_path, to_path, e)
            return False
        except (OSError, ftplib.Error) as e:
            logger.warning("Failed to rename %s -> %s: %s", from_path, to_path, e)
            return False

    def get_file_size(self, remote_path: str) -> int | None:
        """Get the size of a file."""
        if not self._ftp:
            return None

        try:
            return self._ftp.size(remote_path)
        except (OSError, ftplib.Error):
            return None

    def get_storage_info(self) -> dict | None:
        """Get storage information from the printer."""
        if not self._ftp:
            return None

        result = {}

        # Try AVBL command (available space) - some FTP servers support this
        try:
            response = self._ftp.sendcmd("AVBL")
            logger.debug("AVBL response: %s", response)
            # Response format: "213 <bytes available>"
            if response.startswith("213"):
                parts = response.split()
                if len(parts) >= 2:
                    result["free_bytes"] = int(parts[1])
        except (OSError, ftplib.Error) as e:
            logger.debug("AVBL command not supported: %s", e)
            # Try STAT command as fallback
            try:
                response = self._ftp.sendcmd("STAT")
                logger.debug("STAT response: %s", response)
            except (OSError, ftplib.Error):
                pass  # Both AVBL and STAT unsupported; storage info will rely on directory scan

        # Calculate used space by listing root directories
        try:
            total_used = 0
            dirs_to_scan = ["/cache", "/timelapse", "/model", "/data", "/data/Metadata", "/"]

            for dir_path in dirs_to_scan:
                try:
                    self._ftp.cwd(dir_path)
                    items = []
                    self._ftp.retrlines("LIST", items.append)

                    for item in items:
                        parts = item.split()
                        if len(parts) >= 5 and not item.startswith("d"):
                            try:
                                total_used += int(parts[4])
                            except ValueError:
                                pass  # Skip entries with non-numeric size fields
                except (OSError, ftplib.Error):
                    pass  # Directory may not exist on this printer model; skip it

            result["used_bytes"] = total_used
        except (OSError, ftplib.Error):
            pass  # Storage scan failed; return whatever info was collected above

        return result if result else None


async def download_file_async(
    ip_address: str,
    access_code: str,
    remote_path: str,
    local_path: Path,
    timeout: float = 60.0,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
) -> bool:
    """Download one file to ``local_path`` on the printer's FTP lane.

    For A1/A1 Mini printers, tries prot_p first, then falls back to prot_c; the
    working mode is cached for future operations.

    Args:
        timeout: how long to wait for the lane and a worker slot (#2572) — NOT a
            cap on the transfer. A 21.6 MB 3MF off a P1S needs 96 s at 231 KB/s;
            a stall is caught by ``socket_timeout`` on every recv, which is what
            a stall actually is (BambuStudio: CONNECTTIMEOUT 10, TIMEOUT 0).
        socket_timeout: FTP socket timeout for slow connections (e.g., A1 printers)
        printer_model: Printer model for A1-specific workarounds
    """
    is_a1 = printer_model in BambuFTPClient.A1_MODELS if printer_model else False

    def _download(force_prot_c: bool):
        mode_str = "prot_c" if force_prot_c else "prot_p"

        def target(worker: LaneWorker) -> bool:
            client = BambuFTPClient(
                ip_address,
                access_code,
                timeout=socket_timeout,
                printer_model=printer_model,
                force_prot_c=force_prot_c,
            )
            if not _session(worker, client):
                return False
            try:
                result = client.download_to_file(remote_path, local_path)
                if result:
                    BambuFTPClient.cache_mode(ip_address, mode_str)
                return result
            except Exception as exc:
                if _cancel_caused(worker, exc):
                    return False
                raise
            finally:
                _end_session(worker, client)

        return target

    async def _run(force_prot_c: bool) -> bool:
        try:
            return await printer_file_runtime.call(
                ftp_key(ip_address), "download", _download(force_prot_c), admission_timeout=timeout
            )
        except LaneRefused as refused:
            logger.warning("FTP download of %s not started (%s)", remote_path, refused.reason)
            return False
        except WorkerCancelled:
            return False

    cached_mode = BambuFTPClient._mode_cache.get(ip_address)
    if cached_mode:
        return await _run(cached_mode == "prot_c")

    if await _run(False):
        return True

    if is_a1:
        logger.info("FTP download failed with prot_p for A1 model, trying prot_c fallback...")
        return await _run(True)

    return False


async def download_file_try_paths_async(
    ip_address: str,
    access_code: str,
    remote_paths: list[str],
    local_path: Path,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    timeout: float = 90.0,
) -> bool:
    """Whether :func:`download_file_first_path_async` found any of the paths -- the old bool contract.

    Deliberately NOT serialized itself: the call below holds the printer's FTP lock for its one session, and a
    second acquisition of the same lock here would never be granted.
    """
    found = await download_file_first_path_async(
        ip_address,
        access_code,
        remote_paths,
        local_path,
        socket_timeout=socket_timeout,
        printer_model=printer_model,
        timeout=timeout,
    )
    return found is not None


async def download_file_first_path_async(
    ip_address: str,
    access_code: str,
    remote_paths: list[str],
    local_path: Path,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    timeout: float = 90.0,
) -> str | None:
    """Try downloading a file from multiple paths using a single connection; the path that worked, or None.

    The remote path is the one name the transport has proved: which of the candidates the bytes in
    ``local_path`` really came from (swap-source-lifecycle spec r4 §7). The local file's own name proves nothing.

    Args:
        socket_timeout: FTP socket timeout for slow connections (e.g., A1 printers)
        printer_model: Printer model for A1-specific workarounds
        timeout: how long to wait for the lane and a worker slot (#2572); never a cap on the transfer.
    """
    return (
        await download_first_path(
            ip_address,
            access_code,
            remote_paths,
            local_path,
            socket_timeout=socket_timeout,
            printer_model=printer_model,
            timeout=timeout,
        )
    ).path


@dataclass(frozen=True)
class FirstPathResult:
    """What one walk over candidate paths proved (G10 A7).

    ``path`` — the candidate whose bytes are in the local file, or None.
    ``not_readable`` — candidates the printer answered 550 for (missing OR forbidden: 550 proves neither).
    ``transfer_failed`` — the candidate whose transfer started and failed; the walk stopped there,
    because the session it ran on is no longer known to be healthy.
    ``reason`` — why nothing was downloaded: ``unavailable`` (no session), ``transfer_failed``,
    ``not_readable`` (every candidate 550), ``busy`` / ``restoring`` (never started), ``cancelled``.
    """

    path: str | None
    not_readable: tuple[str, ...] = ()
    transfer_failed: str | None = None
    reason: str | None = None


async def download_first_path(
    ip_address: str,
    access_code: str,
    remote_paths: list[str],
    local_path: Path,
    *,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    timeout: float = 90.0,
) -> FirstPathResult:
    """Walk ``remote_paths`` over ONE session; stop at the first success or the first transport failure."""

    def target(worker: LaneWorker) -> FirstPathResult:
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            return FirstPathResult(None, reason="unavailable")
        not_readable: list[str] = []
        try:
            for remote_path in remote_paths:
                worker.checkpoint()
                try:
                    if client.download_to_file(remote_path, local_path):
                        return FirstPathResult(remote_path, tuple(not_readable))
                except FileNotOnPrinterError:
                    # 550: not readable at this path — try the next candidate on the same session.
                    not_readable.append(remote_path)
                    continue
                except Exception as exc:
                    if _cancel_caused(worker, exc):
                        return FirstPathResult(None, tuple(not_readable), reason="cancelled")
                    raise
                if worker.cancel_requested:
                    return FirstPathResult(None, tuple(not_readable), reason="cancelled")
                # The transfer started and failed (short read, dropped data channel, stall): the
                # session may be dead, and walking on would cost a socket timeout per candidate.
                return FirstPathResult(None, tuple(not_readable), transfer_failed=remote_path, reason="transfer_failed")
            return FirstPathResult(None, tuple(not_readable), reason="not_readable")
        finally:
            _end_session(worker, client)

    try:
        return await printer_file_runtime.call(ftp_key(ip_address), "download-first", target, admission_timeout=timeout)
    except LaneRefused as refused:
        logger.warning("FTP download walk for %s not started (%s)", ip_address, refused.reason)
        return FirstPathResult(None, reason=refused.reason)
    except WorkerCancelled:
        return FirstPathResult(None, reason="cancelled")


async def upload_file_async(
    ip_address: str,
    access_code: str,
    local_path: Path,
    remote_path: str,
    timeout: float = 600.0,
    progress_callback: Callable[[int, int], None] | None = None,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    report: UploadReport | None = None,
    *,
    may_run: Callable[[], bool] | None = None,
) -> bool:
    """Async wrapper for uploading a file with timeout and progress callback.

    ``report`` — the caller's :class:`UploadReport`, filled with why the upload
    failed (cleared on success); see :func:`describe_upload_failure`.

    ``may_run`` is asked after the wait for the printer's file channel and again
    after the connection, immediately before the first byte (G10 B): a STOR over
    a name destroys the file under it exactly as a DELE would.

    For A1/A1 Mini printers, automatically tries prot_p first, then falls back
    to prot_c if the upload fails. The working mode is cached for future uploads.

    Args:
        ip_address: Printer IP address
        access_code: Printer access code
        local_path: Local file path to upload
        remote_path: Remote path on printer
        timeout: Overall operation timeout (asyncio)
        progress_callback: Optional callback for progress updates
        socket_timeout: FTP socket timeout for slow connections (e.g., A1 printers)
        printer_model: Printer model for A1-specific workarounds
    """
    is_a1 = printer_model in BambuFTPClient.A1_MODELS if printer_model else False

    def _upload(force_prot_c: bool):
        mode_str = "prot_c" if force_prot_c else "prot_p"

        def target(worker: LaneWorker) -> bool:
            if may_run is not None and not may_run():
                return False
            logger.info(
                f"FTP connecting to {ip_address} for upload (model={printer_model}, "
                f"mode={mode_str}, socket_timeout={socket_timeout}s)..."
            )
            client = BambuFTPClient(
                ip_address, access_code, timeout=socket_timeout, printer_model=printer_model, force_prot_c=force_prot_c
            )
            if not _session(worker, client):
                logger.warning("FTP connection failed to %s", ip_address)
                if report is not None:
                    report.failure = client.failure or FtpFailure("network")
                return False
            logger.info("FTP connected to %s", ip_address)
            try:
                if may_run is not None and not may_run():
                    return False
                result = client.upload_file(local_path, remote_path, progress_callback)
                if result:
                    BambuFTPClient.cache_mode(ip_address, mode_str)
                if report is not None:
                    report.failure = None if result else client.failure
                return result
            finally:
                _end_session(worker, client)

        return target

    async def _run(force_prot_c: bool) -> bool:
        """One upload attempt; the lane stays owned by its thread until that thread ends (#2529).

        A slow upload is never retried on top of itself: the next attempt (with_ftp_retry, or the
        A1 fallback) waits for this thread to end before it opens a session. Slow WiFi commonly
        overshoots ``timeout`` by 10-30 s without being stuck, so a thread that completes within
        the grace window is still a success; one that does not is asked to stop (its sockets are
        closed) and the attempt fails — its lane is released when the thread really ends.
        """
        from backend.app.services.farm_startup_guard import track_command_io

        try:
            async with printer_file_runtime.hold(ftp_key(ip_address)) as lane:
                worker = await printer_file_runtime.start(lane, "upload", _upload(force_prot_c))
                track_command_io(worker.future)
                try:
                    return await printer_file_runtime.wait(worker, timeout)
                except LaneTimeout:
                    grace = max(min(timeout, 30.0), 0.5)
                    try:
                        result = await printer_file_runtime.wait(worker, grace)
                    except LaneTimeout:
                        worker.request_cancel("timeout")
                        logger.warning(
                            "FTP upload timed out after %ss (plus %ss grace) for %s", timeout, grace, remote_path
                        )
                        if report is not None:
                            report.failure = FtpFailure("stalled", f"not finished after {timeout}s")
                        return False
                    if result:
                        logger.info(
                            "FTP upload outlived its %ss wait for %s, but its thread completed within %ss grace — "
                            "salvaging",
                            timeout,
                            remote_path,
                            grace,
                        )
                    return result
        except LaneRefused as refused:
            logger.warning("FTP upload of %s not started (%s)", remote_path, refused.reason)
            if report is not None:
                report.failure = FtpFailure("network", f"not started: {refused.reason}")
            return False
        except WorkerCancelled:
            if report is not None:
                report.failure = FtpFailure("network", "cancelled before connecting")
            return False

    # Check if we have a cached mode for this printer
    cached_mode = BambuFTPClient._mode_cache.get(ip_address)

    if cached_mode:
        # Use cached mode
        return await _run(cached_mode == "prot_c")

    # No cached mode - try prot_p first
    if await _run(False):
        return True

    # Upload failed - for A1 models, try prot_c fallback
    if is_a1:
        logger.info("FTP upload failed with prot_p for A1 model, trying prot_c fallback...")
        return await _run(True)

    return False


async def download_to_path_async(
    ip_address: str,
    access_code: str,
    remote_path: str,
    local_path: Path,
    *,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    admission_timeout: float | None = None,
    byte_budget: int | None = None,
    reserve_bytes: int = 0,
    progress: Callable[[int], None] | None = None,
):
    """Stream one file to ``local_path`` on the printer's FTP lane; a typed :class:`TransferOutcome`.

    No cap on a healthy transfer: a stall is the socket idle deadline
    (``socket_timeout``). ``admission_timeout`` bounds only the wait for the lane
    and a worker slot. A cancelled caller asks the worker to stop; the lane is
    released when its thread has ended.
    """
    from backend.app.services.printer_files.base import TRANSFER_BUSY, TRANSFER_CANCELLED, TransferOutcome

    def target(worker: LaneWorker):
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            kind = client.failure.kind if client.failure else "connect"
            return TransferOutcome("unavailable", reason=kind)
        try:
            status, received, check, reason = client.fetch_to_file(
                remote_path, local_path, byte_budget=byte_budget, reserve_bytes=reserve_bytes, progress=progress
            )
            if status != "completed" and worker.cancel_requested:
                return TransferOutcome(TRANSFER_CANCELLED, received, reason=worker.cancel_reason)
            return TransferOutcome(status, received, check, reason)
        except Exception as exc:
            # The file is already gone (fetch_to_file discards before it raises).
            if _cancel_caused(worker, exc):
                return TransferOutcome(TRANSFER_CANCELLED, reason=worker.cancel_reason)
            raise
        finally:
            _end_session(worker, client)

    try:
        return await _on_lane(ip_address, "fetch", target, admission_timeout=admission_timeout)
    except LaneRefused as refused:
        return TransferOutcome(TRANSFER_BUSY, reason=refused.reason)
    except WorkerCancelled as cancelled:
        return TransferOutcome(TRANSFER_CANCELLED, reason=str(cancelled))


async def list_directory_async(
    ip_address: str,
    access_code: str,
    path: str = "/",
    timeout: float = 30.0,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    *,
    prove_missing: bool = False,
) -> DirectoryListing:
    """List one directory on the printer's FTP lane, with a typed outcome.

    ``timeout`` bounds the caller's wait: when it runs out the answer is
    ``slow`` — the worker is asked to stop and still owns the lane until its
    thread has ended. ``prove_missing`` turns an ambiguous 550 into ``missing``
    only through a complete parent listing on the same session.
    """

    def target(worker: LaneWorker) -> DirectoryListing:
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            return DirectoryListing(
                path, LISTING_UNAVAILABLE, reason=client.failure.kind if client.failure else "connect"
            )
        try:
            return client.list_directory_proving(path) if prove_missing else client.list_directory(path)
        finally:
            _end_session(worker, client)

    try:
        return await _on_lane(ip_address, "list", target, timeout=timeout)
    except LaneTimeout:
        logger.warning("FTP list_files timed out after %ss for %s", timeout, path)
        return DirectoryListing(path, LISTING_SLOW, reason="timeout")
    except LaneRefused as refused:
        return DirectoryListing(path, LISTING_UNAVAILABLE, reason=refused.reason)
    except WorkerCancelled:
        return DirectoryListing(path, LISTING_UNAVAILABLE, reason="cancelled")


async def list_files_checked_async(
    ip_address: str,
    access_code: str,
    path: str = "/",
    timeout: float = 30.0,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
) -> tuple[list[dict], bool]:
    """List a directory, and say whether the printer ANSWERED for it.

    Returns ``(entries, answered)``. ``answered`` is True for a complete LIST,
    and for a directory proven absent by a complete listing of its parent — an
    absent ``/record`` is an answer, not silence (G10 CR2). A refused connect, a
    550 the parent cannot settle (missing and forbidden read the same), a failed
    or partial LIST and a timeout are all ``False``; a caller walking several
    directories stops on those. Use :func:`list_directory_async` for the typed
    outcome.
    """
    listing = await list_directory_async(
        ip_address,
        access_code,
        path,
        timeout=timeout,
        socket_timeout=socket_timeout,
        printer_model=printer_model,
        prove_missing=True,
    )
    return list(listing.entries), listing.status in (LISTING_COMPLETE, LISTING_MISSING)


async def list_files_async(
    ip_address: str,
    access_code: str,
    path: str = "/",
    timeout: float = 30.0,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
) -> list[dict]:
    """Async wrapper for listing files with timeout.

    Keeps the plain-list shape every existing caller expects. Where the
    difference between "empty" and "unreachable" matters — anything that walks
    more than one directory — use :func:`list_files_checked_async` instead.

    Args:
        socket_timeout: FTP socket timeout for slow connections (e.g., A1 printers)
        printer_model: Printer model for A1-specific workarounds
    """
    entries, _answered = await list_files_checked_async(
        ip_address,
        access_code,
        path,
        timeout=timeout,
        socket_timeout=socket_timeout,
        printer_model=printer_model,
    )
    return entries


async def delete_file_async(
    ip_address: str,
    access_code: str,
    remote_path: str,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    timeout: float = 60.0,
    *,
    may_run: Callable[[], bool] | None = None,
) -> DeleteResult:
    """Async wrapper for deleting a file.

    Returns :class:`DeleteResult` so callers can distinguish ``NOT_FOUND``
    (550 — file isn't on the printer, no retry value) from ``FAILED``
    (network / auth / transient — worth retrying or surfacing).

    Args:
        socket_timeout: FTP socket timeout for slow connections (e.g., A1 printers)
        printer_model: Printer model for A1-specific workarounds
        timeout: overall async cap so a saturated executor can't pin the caller
            (and any DB connection it holds) indefinitely (#2572).
    """

    def target(worker: LaneWorker) -> DeleteResult:
        if may_run is not None and not may_run():
            return DeleteResult.FAILED
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            return DeleteResult.FAILED
        try:
            if may_run is not None and not may_run():
                return DeleteResult.FAILED
            return client.delete_file(remote_path)
        finally:
            _end_session(worker, client)

    try:
        return await _on_lane(ip_address, "delete", target, timeout=timeout)
    except LaneTimeout:
        logger.warning("FTP delete_file exceeded its %ss cap for %s (#2572)", timeout, ip_address)
        return DeleteResult.FAILED
    except LaneRefused as refused:
        logger.warning("FTP delete of %s not started (%s)", remote_path, refused.reason)
        return DeleteResult.FAILED
    except WorkerCancelled:
        return DeleteResult.FAILED


async def clear_sdcard_async(
    ip_address: str,
    access_code: str,
    root: str = "/",
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    *,
    may_run: Callable[[], bool] | None = None,
) -> dict:
    """Async wrapper for a full recursive SD-card wipe.

    Runs the whole walk inside one executor job (one FTP connection). No
    asyncio ``wait_for`` cap — a card with many files legitimately takes a
    while and the caller is an explicit, confirmed admin action.

    Returns ``{"deleted", "failed", "connected", "stopped"}`` so the caller can
    tell a failed connection (nothing wiped) apart from "nothing to delete", and
    a walk ``may_run`` stopped part-way from a finished one.
    """
    nothing = {"deleted": 0, "failed": 0, "folders_removed": 0, "stopped": False}

    def target(worker: LaneWorker) -> dict:
        # Asked after the lane wait and before the connection: a print that
        # started while this waited must not find its card being emptied.
        if may_run is not None and not may_run():
            return {**nothing, "connected": True, "stopped": True}
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            return {**nothing, "connected": False}
        try:
            result = client.clear_all(root, may_run)
            result["connected"] = True
            return result
        finally:
            _end_session(worker, client)

    try:
        return await _on_lane(ip_address, "clear-sdcard", target)
    except (LaneRefused, WorkerCancelled):
        return {**nothing, "connected": False}


async def rename_file_async(
    ip_address: str,
    access_code: str,
    from_path: str,
    to_path: str,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    *,
    may_run: Callable[[], bool] | None = None,
) -> bool | None:
    """Async wrapper for renaming/moving a file. Returns True on success, None if not found, False on error."""

    def target(worker: LaneWorker):
        if may_run is not None and not may_run():
            return False
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            return False
        try:
            if may_run is not None and not may_run():
                return False
            return client.rename_file(from_path, to_path)
        finally:
            _end_session(worker, client)

    try:
        return await _on_lane(ip_address, "rename", target)
    except (LaneRefused, WorkerCancelled):
        return False


async def download_file_bytes_async(
    ip_address: str,
    access_code: str,
    remote_path: str,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    timeout: float = 300.0,
) -> bytes | None:
    """Download one whole file into memory, on the printer's FTP lane.

    Args:
        socket_timeout: FTP socket timeout for slow connections (e.g., A1 printers)
        printer_model: Printer model for A1-specific workarounds
        timeout: how long to wait for the lane and a worker slot (#2572). It is
            NOT a cap on the transfer (upstream #3272): a large timelapse over
            slow Wi-Fi legitimately takes minutes, and a flat cap both failed it
            and left its thread running beside the next session. A stall is
            caught by ``socket_timeout`` on every recv.

    ⚠️ The whole file is held in memory. New consumers stream to a file instead.
    """

    def target(worker: LaneWorker):
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            return None
        try:
            return client.download_file(remote_path)
        finally:
            _end_session(worker, client)

    try:
        return await _on_lane(ip_address, "read", target, admission_timeout=timeout)
    except LaneRefused as refused:
        logger.warning("FTP read of %s not started (%s) (#2572)", remote_path, refused.reason)
        return None
    except WorkerCancelled:
        return None


async def upload_bytes_async(
    ip_address: str,
    access_code: str,
    data: bytes,
    remote_path: str,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    *,
    may_run: Callable[[], bool] | None = None,
) -> bool:
    """Async wrapper for uploading bytes to a file on the printer."""

    def target(worker: LaneWorker) -> bool:
        if may_run is not None and not may_run():
            return False
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            return False
        try:
            if may_run is not None and not may_run():
                return False
            return client.upload_bytes(data, remote_path)
        finally:
            _end_session(worker, client)

    try:
        return await _on_lane(ip_address, "upload-bytes", target)
    except (LaneRefused, WorkerCancelled):
        return False


async def get_storage_info_async(
    ip_address: str,
    access_code: str,
    socket_timeout: float | None = None,
    printer_model: str | None = None,
    timeout: float = 60.0,
) -> dict | None:
    """Async wrapper for getting storage info.

    Args:
        socket_timeout: FTP socket timeout for slow connections (e.g., A1 printers)
        printer_model: Printer model for A1-specific workarounds
        timeout: overall async cap so a saturated executor can't pin the caller
            (and any DB connection it holds) indefinitely (#2572).
    """

    def target(worker: LaneWorker):
        client = BambuFTPClient(ip_address, access_code, timeout=socket_timeout, printer_model=printer_model)
        if not _session(worker, client):
            return None
        try:
            return client.get_storage_info()
        finally:
            _end_session(worker, client)

    try:
        return await _on_lane(ip_address, "storage", target, timeout=timeout)
    except LaneTimeout:
        logger.warning("FTP get_storage_info exceeded its %ss cap for %s (#2572)", timeout, ip_address)
        return None
    except (LaneRefused, WorkerCancelled):
        return None


async def get_ftp_retry_settings() -> tuple[bool, int, float, float]:
    """Get FTP retry settings from database.

    Returns:
        Tuple of (retry_enabled, retry_count, retry_delay, timeout)
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.core.database import async_session

    async with async_session() as db:
        enabled = (await get_setting(db, "ftp_retry_enabled") or "true") == "true"
        count = int(await get_setting(db, "ftp_retry_count") or "3")
        delay = float(await get_setting(db, "ftp_retry_delay") or "2")
        timeout = float(await get_setting(db, "ftp_timeout") or "30")
    return enabled, count, delay, timeout


async def with_ftp_retry(
    operation: Callable[..., Awaitable[T]],
    *args,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    operation_name: str = "FTP operation",
    non_retry_exceptions: tuple[type[BaseException], ...] = (),
    **kwargs,
) -> T | None:
    """Execute FTP operation with retry logic.

    Args:
        operation: Async function to execute
        *args: Positional arguments for the operation
        max_retries: Number of retry attempts (default: 3)
        retry_delay: Seconds to wait between retries (default: 2.0)
        operation_name: Name for logging purposes
        non_retry_exceptions: Exception types that should immediately abort retries
        **kwargs: Keyword arguments for the operation

    Returns:
        Result of the operation, or None if all attempts fail
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            result = await operation(*args, **kwargs)
            # Check for "falsy" success indicators
            if result not in (False, None, []):
                if attempt > 0:
                    logger.info("%s succeeded on attempt %s/%s", operation_name, attempt + 1, max_retries + 1)
                return result
            # Operation returned failure indicator
            if attempt > 0:
                logger.info("%s attempt %s/%s returned failure", operation_name, attempt + 1, max_retries + 1)
        except Exception as e:
            if non_retry_exceptions and isinstance(e, non_retry_exceptions):
                raise
            last_error = e
            logger.warning("%s attempt %s/%s failed: %s", operation_name, attempt + 1, max_retries + 1, e)

        # Don't wait after the last attempt. Exponential backoff (capped at
        # 30s) so a printer that's momentarily busy / refusing a second FTP
        # session (P2S right after a print finishes) gets progressively more
        # time to recover instead of being hammered every 2s.
        if attempt < max_retries:
            wait = min(retry_delay * (2**attempt), 30.0)
            logger.info("%s will retry in %.1fs...", operation_name, wait)
            await asyncio.sleep(wait)

    logger.error("%s failed after %s attempts", operation_name, max_retries + 1)
    if last_error:
        logger.debug("Last error: %s", last_error)
    return None
