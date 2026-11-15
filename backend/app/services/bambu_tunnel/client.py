"""A client for the Bambu file tunnel (TCP 6000, TLS).

Read-side only in this stage: handshake, list, read, delete. Upload joins in
the dispatch stage, together with its counterpart on the FTP transport.

⚠️ **Replies are asynchronous.** The live X2D answered a session frame *after*
the next request had already gone out, so a reply is claimed by its
``sequence`` and never by arrival order. Frames belonging to another sequence
are parked until their own waiter asks for them.

⚠️ **Not every frame carries JSON.** The session frame is acknowledged with a
bare four-byte control frame containing no envelope at all. A client that
assumes an envelope dies on the very first thing the printer says.

⚠️ **An open port proves nothing.** On P1S and A1 mini port 6000 accepts a
connection and completes a TLS handshake — that is the camera daemon, and it
answers no tunnel frame. Every wait is therefore bounded, and a timeout is a
normal negative answer rather than a crash.

Protocol reference: vault ``60-specs/bambu-file-tunnel-protocol.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ssl
from collections.abc import Callable
from pathlib import Path

from backend.app.services.bambu_tunnel.codec import (
    ENVELOPE_SEPARATOR,
    HEADER_SIZE,
    TYPE_CONTROL_REQUEST,
    TYPE_DATA_REQUEST,
    EnvelopeTooLong,
    pack_frame,
    parse_header,
    split_envelope_view,
)

logger = logging.getLogger(__name__)

MTYPE_SESSION = 12291  # 0x3003
MTYPE_FILE = 12289  # 0x3001

# ⚠️ Straight from BambuStudio's own enum (PrinterFileSystem.h), and the names
# matter: ``SUB_FILE`` reads a resource INSIDE a container (a thumbnail via
# ``…3mf#Metadata/plate_1.png``) and refuses a whole file with ``result=14``.
# Downloading a file is a different command with a different shape.
CMD_LIST = 1  # LIST_INFO
CMD_SUB_FILE = 2  # SUB_FILE — a member of a container, never the container
CMD_DELETE = 3  # FILE_DEL
CMD_DOWNLOAD = 4  # FILE_DOWNLOAD — a whole file, streamed
CMD_UPLOAD = 5  # FILE_UPLOAD
CMD_HANDSHAKE = 7

DEFAULT_PORT = 6000

# ⚠️ BambuStudio's own ``CONTINUE`` (PrinterFileSystem.h:50). In a REPLY it
# means "this frame is not the last"; in an upload REQUEST it means "more to
# come". Same word, both directions — and treating it as an error makes every
# streamed download fail on its very first frame.
RESULT_SUCCESS = 0
RESULT_CONTINUE = 1
# ⚠️ ``FILE_READ_WRITE_ERR``. This is what a plain path gets from SUB_FILE, and
# it is the printer saying "I cannot read that as a member", not "no such file".
RESULT_FILE_READ_WRITE_ERR = 14
# The file is already there. BambuStudio treats it exactly like CONTINUE and
# resumes from the offset the printer reports.
RESULT_FILE_EXIST = 19

# 255 KiB — the fragment size BambuStudio uses on the wire.
CHUNK_SIZE = 261120

# ⚠️ Memory is OUR policy, not the protocol's maximum (G10 CR3/CR5, spec r4 §12):
# BambuStudio's own code sets no limit on a download frame (its DLL allocates),
# and the printer was only ever seen sending 255 KiB fragments.
#
# One envelope bounds every Python buffer a client holds for its one active file
# (review A4): the payload of the frame it is working on and what reading and
# parsing it costs, every parked frame, and a copy it hands out. A frame is
# admitted — or refused — from its header, BEFORE anything is allocated for it.
#: Spec r4 §12: ≤ 8 MiB of Python transfer buffers per active file.
TRANSFER_BUFFER_ENVELOPE = 8 * 1024 * 1024
#: What working on one frame costs beside its payload, charged while it is live:
#: one read chunk and the stream's own buffer, the envelope's bytes and text, and
#: the objects its JSON becomes — a 64 KiB envelope of empty lists measured 1.5 MiB.
FRAME_OVERHEAD = 3 * 1024 * 1024
#: The largest frame, envelope included (r4 allows ≤ 8 MiB): 8× the observed
#: fragment, and small enough that a live frame and a copy of its body fit the
#: envelope together (2 + 3 + 2 MiB), with room for parked frames beside a fragment.
MAX_FRAME_PAYLOAD = 2 * 1024 * 1024
#: The envelope is a few hundred bytes; past this a JSON that never closes is refused.
MAX_ENVELOPE_SCAN = 64 * 1024
#: Frames of other sequences kept for their own waiters, by count; their bytes are
#: charged to the same envelope.
MAX_PARKED_FRAMES = 32
#: How much of a payload is read per call, so a frame is never held twice whole.
READ_CHUNK = 256 * 1024
#: How long a graceful close may take before the connection is aborted.
CLOSE_GRACE = 5.0

# ⚠️ Three wire names for two media, and which one applies depends on the
# operation. Reading the internal medium is ``internal`` and the external one is
# the ABSENCE of the key; UPLOADING names them ``emmc`` and ``udisk``. None of
# the three derives from another. Exported so the transport layer does not have
# to spell them itself.
WIRE_INTERNAL = "internal"
WIRE_EMMC = "emmc"
WIRE_UDISK = "udisk"


class TunnelError(Exception):
    """A refused command, a dead peer, or a peer that is not a tunnel at all."""

    def __init__(self, message: str, result: int = -1):
        super().__init__(message)
        self.result = result


class TunnelProtocolError(TunnelError):
    """The peer broke the framing: an oversized or unbounded frame, a gap, a wrong total or checksum.

    ``category`` is fixed and safe to log: ``oversized_frame``, ``buffer_credit``,
    ``envelope_unbounded``, ``too_many_parked``, ``offset_mismatch``, ``total_mismatch``,
    ``overrun``, ``short_transfer``, ``checksum_mismatch``, ``checksum_malformed``.
    """

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


class _Frame:
    """One received frame's payload, charged to its client's envelope until released.

    The payload is kept as received — never copied to park it. While the frame
    is live its charge also covers :data:`FRAME_OVERHEAD` (its parsed envelope);
    a parked frame drops that envelope and is charged its payload alone, and
    the envelope is parsed again when its waiter claims it.
    """

    __slots__ = ("_payload", "charge", "start")

    def __init__(self, payload: bytearray, start: int, charge: int):
        self._payload: bytearray | None = payload
        self.start = start
        self.charge = charge

    @property
    def payload_len(self) -> int:
        return len(self._payload) if self._payload is not None else 0

    @property
    def body_len(self) -> int:
        return self.payload_len - self.start if self._payload is not None else 0

    def body(self) -> memoryview:
        """A view of the body, valid until the caller releases it — never kept past the frame."""
        return memoryview(self._payload)[self.start :]

    def envelope(self) -> dict:
        envelope, body = split_envelope_view(self._payload, MAX_ENVELOPE_SCAN)
        body.release()
        return envelope

    def drop(self) -> None:
        self._payload = None


class DownloadReceipt:
    """What a streamed download proved: how many bytes, and how they were checked."""

    __slots__ = ("checksum", "md5", "total")

    def __init__(self, total: int, checksum: str, md5: str):
        self.total = total
        # ``checksum_verified`` — the printer's file_md5 matched; ``length_verified`` —
        # no file_md5 was sent, so only the length proves the transfer.
        self.checksum = checksum
        self.md5 = md5


def _tls_context() -> ssl.SSLContext:
    """The printer's certificate is self-signed and it asks for no client cert.

    Same reasoning as ``ImplicitFTP_TLS`` in ``bambu_ftp.py`` and the MQTT
    context in ``bambu_mqtt.py``, to the same devices on the same LAN: there is
    no authority to verify this certificate against, and the access code is
    what authenticates. Verification is off because there is nothing to verify,
    not because it was inconvenient.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def _close_writer(writer, grace: float) -> None:
    """Close ``writer`` and wait until its transport reports the connection lost (review A1).

    Gracefully first; a cancel — any number of them — or ``grace`` running out
    aborts the transport instead, and the wait goes on until the loss is
    reported. A writer without an abortable transport is simply waited for.
    The cancel is raised once the connection is really gone.
    """
    loop = asyncio.get_running_loop()
    writer.close()
    closed = asyncio.ensure_future(writer.wait_closed())
    deadline = loop.time() + grace
    cancelled = aborted = False
    while not closed.done():
        try:
            await asyncio.wait({closed}, timeout=None if aborted else max(0.0, deadline - loop.time()))
        except asyncio.CancelledError:
            cancelled = True
        if not closed.done() and not aborted and (cancelled or loop.time() >= deadline):
            aborted = True
            transport = getattr(writer, "transport", None)
            if transport is not None:
                transport.abort()
    error = None if closed.cancelled() else closed.exception()
    if error is not None and not isinstance(error, (OSError, ssl.SSLError)):
        raise error
    if cancelled:
        raise asyncio.CancelledError


class BambuTunnelClient:
    def __init__(
        self,
        ip: str,
        access_code: str,
        *,
        port: int = DEFAULT_PORT,
        timeout: float = 8.0,
        connector=None,
        close_grace: float = CLOSE_GRACE,
    ):
        self._ip = ip
        self._access_code = access_code
        self._port = port
        self._timeout = timeout
        self._close_grace = close_grace
        # Tests inject a plain-TCP connector so no certificate is involved;
        # production leaves this None and we build the TLS connection here.
        self._connector = connector
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._sequence = 0
        # ⚠️ A LIST of frames per sequence, not one. A download answers a single
        # request with many frames that all carry the same sequence, and a plain
        # dict would keep only the last — silently dropping the middle of a file.
        self._parked: dict[int, list[_Frame]] = {}
        self._parked_count = 0
        self._parked_bytes = 0
        # Every byte charged to this client's envelope: live frames and parked ones.
        self._held = 0

    async def __aenter__(self) -> BambuTunnelClient:
        try:
            await self.connect()
        except BaseException:
            # A connect cut short after the socket opened still owns that socket.
            await self.close()
            raise
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the socket, authenticate, and open a session.

        ⚠️ Neither the auth frame nor the session frame is awaited. The live
        printer acknowledged the session with a bare control frame and sent its
        JSON reply only later, after the next request had gone out — a client
        that blocks here waits for something that arrives on somebody else's
        schedule. The handshake is the first thing we actually wait for, and a
        rejected access code surfaces there as a closed connection.
        """
        if self._connector is not None:
            self._reader, self._writer = await self._connector()
        else:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._ip, self._port, ssl=_tls_context()),
                timeout=self._timeout,
            )

        # ⚠️ 16 bytes, no JSON. The camera channel on this same port takes the
        # same credentials in a 32+32 layout with a different mtype; mixing the
        # two is what made the first probes fail with a correct access code.
        self._send(TYPE_CONTROL_REQUEST, 0, b"bblp" + b"\x00" * 4 + self._access_code.encode())

        sequence = self._next_sequence()
        self._send(
            TYPE_DATA_REQUEST,
            sequence,
            json.dumps(
                {
                    "sequence": sequence,
                    "mtype": MTYPE_SESSION,
                    "req": {"t_av": 0, "mtype": MTYPE_FILE, "peer_t": 1, "pid": "", "ver": ""},
                }
            ).encode(),
        )

    async def close(self) -> None:
        """Close the connection; return only once the transport has let go of the socket.

        ⚠️ A cancel does not shorten this (review A1). The operation that owns
        this client also owns its printer lane, and it is the end of this call
        that lets the lane go: returning on a second cancel — a restore's, after
        the caller's — left the lane free over a socket still closing. So a
        cancel, or :data:`CLOSE_GRACE` running out, only turns the graceful
        close into an abort of the transport, and this still waits until the
        transport reports the connection lost; the cancel is raised afterwards.
        A transport that cannot be aborted is waited for, and the owner stays.
        """
        writer = self._writer
        try:
            if writer is not None:
                await _close_writer(writer, self._close_grace)
        finally:
            self._writer = None
            self._reader = None
            for frames in self._parked.values():
                for frame in frames:
                    self._release(frame)
            self._parked.clear()
            self._parked_count = 0
            self._parked_bytes = 0

    async def handshake(self) -> dict:
        """``cmdtype 7`` — what this printer says it can do.

        The reply names the printer's own storages, which is where those names
        must come from: they are never hardcoded.
        """
        return await self._command(CMD_HANDSHAKE, {"api_version": 3, "peer": "studio", "peer_t": 1})

    async def list_files(self, storage: str | None, file_type: str = "model") -> list[dict]:
        """One request, one reply — exactly the shape BambuStudio sends.

        ⚠️ **Do not invent paging.** The reply carries a ``start`` field, but no
        captured Studio request has ``start`` or any other cursor, so how a
        truncated listing would be continued is UNKNOWN. Writing a continuation
        would be designing against an imagined protocol. If a printer ever
        reports a non-zero ``start``, the warning below is the signal to go and
        capture the real continuation rather than to assume one.
        """
        request: dict = {"api_version": 2, "notify": "DETAIL", "type": file_type}
        # ⚠️ The external medium is addressed by the ABSENCE of the key, not by
        # a value. Four spellings of two storages, none derived from another.
        if storage is not None:
            request["storage"] = storage

        reply = await self._command(CMD_LIST, request)
        if reply.get("start"):
            logger.warning(
                "[%s] tunnel listing reported start=%s — the list may be truncated, and the "
                "continuation protocol has never been captured",
                self._ip,
                reply.get("start"),
            )
        entries = reply.get("file_lists") or []

        # ⚠️ Old firmware answers ANY catalogue request with timelapses.
        # BambuStudio detects it exactly this way — ``if (type > F_TIMELAPSE &&
        # path.empty()) return FILE_TYPE_ERR`` in PrinterFileSystem.cpp, with
        # the comment "Fix old printer that always return timelapses". Without
        # the check a browser shows timelapse recordings labelled as models.
        if file_type != "timelapse" and any(not e.get("path") for e in entries):
            raise TunnelError(f"printer does not serve the {file_type!r} catalogue (it answered with timelapses)")

        return entries

    async def read_sub_file(self, path: str, storage: str | None) -> bytes:
        """``SUB_FILE`` — a resource INSIDE a container, addressed with ``#``.

        ⚠️ Not for whole files. The printer answers ``result=14`` when handed a
        plain path, which is exactly what BambuStudio's naming says: this reads
        a member (``…gcode.3mf#Metadata/plate_1.png``), never the container.
        Use :meth:`download_file` for the file itself.
        """
        request: dict = {"paths": [path], "zip": False}
        if storage is not None:
            request["storage"] = storage
        _reply, body = await self._command_with_body(CMD_SUB_FILE, request)
        return body

    async def download_file(self, path: str) -> bytes:
        """``FILE_DOWNLOAD`` into memory — :meth:`download_into` with an in-memory sink.

        ⚠️ The whole file is held in memory; new consumers stream to a file.
        """
        chunks: list[bytes] = []

        async def collect(chunk: memoryview) -> None:
            chunks.append(bytes(chunk))

        await self.download_into(path, collect)
        return b"".join(chunks)

    async def download_into(self, path: str, sink) -> DownloadReceipt:
        """``FILE_DOWNLOAD`` — a whole file, streamed frame by frame into ``sink``.

        One request, then a run of replies that all carry the SAME sequence,
        each with ``offset`` / ``total`` / ``size`` in the envelope and a slice
        of the file as the body. BambuStudio writes each slice to disk as it
        comes and checks the final ``file_md5`` (``PrinterFileSystem::
        DownloadNextFile``); so do we. ``sink`` is awaited per slice and may
        raise to stop the transfer. ⚠️ The slice is a view of the frame, valid
        only while ``sink`` runs: it is released, with the frame and its charge,
        before the next frame is admitted, so two frames are never held at once.

        Every frame is checked: its ``offset`` is where we are, its ``size`` is
        its body, the ``total`` never changes, nothing runs past it. A gap, an
        overlap or a wrong checksum is a :class:`TunnelProtocolError` — never a
        shorter file presented as whole.
        """
        sequence = self._next_sequence()
        self._send(
            TYPE_DATA_REQUEST,
            sequence,
            json.dumps(
                {"mtype": MTYPE_FILE, "cmdtype": CMD_DOWNLOAD, "sequence": sequence, "req": {"path": path}}
            ).encode(),
        )

        digest = hashlib.md5(usedforsecurity=False)  # the printer's choice of checksum
        received = 0
        total: int | None = None
        file_md5: str | None = None

        while True:
            envelope, frame = await self._await_sequence(sequence)
            try:
                result = envelope.get("result", 0)
                # ⚠️ ``1`` is CONTINUE, not a refusal. Only something else is.
                if result not in (0, RESULT_CONTINUE):
                    raise TunnelError(f"tunnel refused the download of {path} with result={result}", result)

                reply = envelope.get("reply") or {}
                size = frame.body_len
                frame_total = int(reply.get("total") or 0)
                if total is None:
                    total = frame_total
                elif frame_total and frame_total != total:
                    raise TunnelProtocolError("total_mismatch", f"tunnel changed the total of {path} mid-transfer")
                if "offset" in reply and int(reply.get("offset") or 0) != received:
                    raise TunnelProtocolError("offset_mismatch", f"tunnel sent {path} out of order")
                if "size" in reply and int(reply.get("size") or 0) != size:
                    raise TunnelProtocolError("offset_mismatch", f"tunnel frame of {path} is not its stated size")
                if total and received + size > total:
                    raise TunnelProtocolError("overrun", f"tunnel sent more of {path} than its total")
                if reply.get("file_md5"):
                    file_md5 = str(reply["file_md5"])

                if size:
                    body = frame.body()
                    try:
                        digest.update(body)
                        await sink(body)
                    finally:
                        body.release()
                received += size
            finally:
                # Before the next frame is admitted (review A4): its envelope's
                # objects and its payload go with its charge.
                envelope = reply = None
                self._release(frame)

            if total and received >= total:
                break
            if result != RESULT_CONTINUE:
                # The printer marked this frame final. Believe it — and check it below.
                break

        if total and received != total:
            raise TunnelProtocolError("short_transfer", f"tunnel sent {received} bytes of {path}, expected {total}")
        ours = digest.hexdigest()
        if file_md5 is None:
            return DownloadReceipt(received, "length_verified", ours)
        if len(file_md5) != 32 or any(c not in "0123456789abcdefABCDEF" for c in file_md5):
            raise TunnelProtocolError("checksum_malformed", f"tunnel sent an unreadable checksum for {path}")
        # Case-insensitive: BambuStudio compares with ``iequals``. No reordering of the
        # digest's words — that belongs to Boost's representation, not the wire (CR3).
        if file_md5.lower() != ours:
            raise TunnelProtocolError("checksum_mismatch", f"tunnel checksum of {path} does not match its bytes")
        return DownloadReceipt(received, "checksum_verified", ours)

    async def delete_files(self, paths: list[str]) -> None:
        await self._command(CMD_DELETE, {"paths": paths})

    async def upload_file(
        self,
        local_path: Path,
        remote_name: str,
        storage: str,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> None:
        """Send a file in three phases: open, fragments, final fragment.

        ⚠️ **``result`` in a REQUEST means "more to come".** It is ``1`` on every
        fragment and ``0`` on the last one — the opposite of ``result`` in a
        reply, where ``0`` is success. Sending ``0`` throughout (the natural
        reflex) declares every fragment final.

        ⚠️ **The envelope and the bytes share one frame.** The chunk begins
        immediately after the closing brace; there is no separate data frame.

        ⚠️ **One sequence for the whole transfer**, with ``frag_id`` identifying
        the chunk. Only the open and the completion are awaited — see below.

        ⚠️ **The storage is ``emmc``/``udisk`` here, not ``internal``.** Upload
        spells the same two media differently from listing.
        """
        total = local_path.stat().st_size
        sequence = self._next_sequence()

        # Phase 1 — the open, awaited: a refusal must stop us before any bytes
        # go on the wire.
        self._send(
            TYPE_DATA_REQUEST,
            sequence,
            json.dumps(
                {
                    "mtype": MTYPE_FILE,
                    "cmdtype": CMD_UPLOAD,
                    "sequence": sequence,
                    "req": {"path": remote_name, "storage": storage, "total": total, "type": "model"},
                }
            ).encode(),
        )
        answer, frame = await self._await_sequence(sequence)
        self._release(frame)
        opened = answer.get("result", 0)
        # ⚠️ The printer answers the open with CONTINUE (1), not SUCCESS.
        # BambuStudio accepts SUCCESS, CONTINUE and FILE_EXIST here and treats
        # anything else as an error — reading CONTINUE as a refusal rejects the
        # normal, expected answer and no upload ever starts.
        if opened not in (RESULT_SUCCESS, RESULT_CONTINUE, RESULT_FILE_EXIST):
            raise TunnelError(f"tunnel refused the upload of {remote_name} with result={opened}", opened)

        open_reply = answer.get("reply") or {}
        if opened == RESULT_SUCCESS:
            # Already complete on the printer's side; BS shows 100% and stops.
            if progress_cb is not None:
                progress_cb(total, total)
            return

        # ⚠️ **The printer chooses the fragment size**, in KILOBYTES, and tells
        # us in this reply — 255 there is where the observed 261120 comes from.
        # It also says where to resume: a re-sent file starts at its ``offset``,
        # not at zero.
        chunk_size = int(open_reply.get("chunk_size") or 0) * 1024 or CHUNK_SIZE
        start_offset = int(open_reply.get("offset") or 0)

        digest = hashlib.md5(usedforsecurity=False)  # the printer's choice of digest, not ours
        sent = start_offset
        frag_id = 0

        with local_path.open("rb") as handle:
            # Resume where the printer said, exactly as BambuStudio does — and
            # like it, the digest covers what we actually send.
            handle.seek(start_offset)
            while True:
                chunk = handle.read(chunk_size)
                digest.update(chunk)
                offset = sent
                sent += len(chunk)
                # An empty file still needs one fragment, or the printer waits
                # for something that never arrives.
                is_last = sent >= total

                req: dict = {"offset": offset, "size": len(chunk)}
                if is_last:
                    # ⚠️ Lowercase here; the same digest goes UPPERCASE into the
                    # MQTT project_file command.
                    req["file_md5"] = digest.hexdigest()

                self._send(
                    TYPE_DATA_REQUEST,
                    sequence,
                    json.dumps(
                        {
                            "cmdtype": CMD_UPLOAD,
                            "mtype": MTYPE_FILE,
                            "sequence": sequence,
                            "frag_id": frag_id,
                            "req": req,
                            # ⚠️ ``1`` is BambuStudio's own ``CONTINUE``
                            # (PrinterFileSystem.h), not a guess from the wire.
                            "result": 0 if is_last else 1,
                        }
                    ).encode()
                    # ⚠️ Framing, not padding: BS writes ``oss << "\n\n"``
                    # between the envelope and the bytes.
                    + ENVELOPE_SEPARATOR
                    + chunk,
                )
                if self._writer is not None:
                    await self._writer.drain()

                frag_id += 1
                if progress_cb is not None:
                    # An exception raised in here travels out untouched — the
                    # dispatcher cancels a job from this callback and must not
                    # have it reported as a transport failure.
                    progress_cb(sent, total)

                if is_last:
                    break

        # ⚠️ Only one await for the whole body. ``_parked`` is keyed by sequence
        # and an upload keeps one throughout, so a per-fragment acknowledgement
        # would be read as the completion. No capture shows the printer sending
        # any, and this log line is what will tell us if one ever does.
        logger.debug("[%s] upload of %s: %d fragment(s) sent, awaiting completion", self._ip, remote_name, frag_id)
        answer, frame = await self._await_sequence(sequence)
        self._release(frame)
        finished = answer.get("result", 0)
        if finished not in (RESULT_SUCCESS, RESULT_FILE_EXIST):
            raise TunnelError(f"tunnel rejected the uploaded {remote_name} with result={finished}", finished)

    # -- plumbing -------------------------------------------------------

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _send(self, type_word: int, sequence: int, body: bytes) -> None:
        if self._writer is None:
            raise TunnelError("tunnel is not connected")
        self._writer.write(pack_frame(type_word, sequence, body))

    async def _command(self, cmdtype: int, request: dict) -> dict:
        reply, _body = await self._command_with_body(cmdtype, request)
        return reply

    async def _command_with_body(self, cmdtype: int, request: dict) -> tuple[dict, bytes]:
        sequence = self._next_sequence()
        envelope = {"mtype": MTYPE_FILE, "cmdtype": cmdtype, "sequence": sequence, "req": request}
        self._send(TYPE_DATA_REQUEST, sequence, json.dumps(envelope).encode())
        answer, frame = await self._await_sequence(sequence)
        try:
            result = answer.get("result", 0)
            if result != 0:
                raise TunnelError(f"tunnel command {cmdtype} refused with result={result}", result)
            reply = answer.get("reply") or {}
            # Callers here get bytes, so the body is copied once — and the copy
            # shares the envelope with the frame it is made from (review A4).
            if self._held + frame.body_len > TRANSFER_BUFFER_ENVELOPE:
                raise TunnelProtocolError("buffer_credit", f"{self._ip} reply does not fit beside its own copy")
            body = frame.body()
            try:
                data = bytes(body)
            finally:
                body.release()
        finally:
            answer = None
            self._release(frame)
        return reply, data

    async def _await_sequence(self, sequence: int) -> tuple[dict, _Frame]:
        """Read frames until the one carrying OUR sequence turns up — a live frame the caller releases.

        Anything else is parked rather than dropped: it belongs to a caller that
        has not asked yet, and discarding it would make the next command read a
        stale answer.
        """
        waiting = self._parked.get(sequence)
        if waiting:
            frame = waiting.pop(0)
            if not waiting:
                del self._parked[sequence]
            self._parked_count -= 1
            self._parked_bytes -= frame.payload_len
            try:
                # Live again: its envelope is parsed once more, under the overhead.
                self._charge(frame, FRAME_OVERHEAD)
                return frame.envelope(), frame
            except BaseException:
                self._release(frame)
                raise

        while True:
            read = await self._read_frame()
            if read is None:
                continue  # a bare control ack, no envelope to match
            envelope, frame = read
            found = envelope.get("sequence")
            if found == sequence:
                return envelope, frame
            envelope = None
            if isinstance(found, int):
                self._park(found, frame)
            else:
                self._release(frame)

    def _park(self, sequence: int, frame: _Frame) -> None:
        """Keep a frame of another sequence for its own waiter — within the bounds (CR5, review A4)."""
        if self._parked_count >= MAX_PARKED_FRAMES:
            self._release(frame)
            raise TunnelProtocolError("too_many_parked", f"{self._ip} sent too many frames nobody asked for")
        # Its payload stays as received (no copy); its parsed envelope is dropped.
        self._uncharge(frame, FRAME_OVERHEAD)
        self._parked.setdefault(sequence, []).append(frame)
        self._parked_count += 1
        self._parked_bytes += frame.payload_len

    def _charge(self, frame: _Frame, amount: int) -> None:
        if self._held + amount > TRANSFER_BUFFER_ENVELOPE:
            raise TunnelProtocolError("buffer_credit", f"{self._ip} frame does not fit beside the data held")
        frame.charge += amount
        self._held += amount

    def _uncharge(self, frame: _Frame, amount: int) -> None:
        amount = min(amount, frame.charge)
        frame.charge -= amount
        self._held -= amount

    def _release(self, frame: _Frame) -> None:
        """Give a frame's charge back and drop its payload. Releasing twice changes nothing."""
        self._uncharge(frame, frame.charge)
        frame.drop()

    async def _read_frame(self) -> tuple[dict, _Frame] | None:
        """One live frame, or ``None`` when it carried no JSON envelope.

        The header's length is checked BEFORE anything is read for it (G10 CR5,
        review A4): over the frame cap, or over what the frames already held
        leave of the envelope together with the cost of working on this one, it
        is refused. The payload is read in chunks into one buffer of its exact
        size, so a frame is never held twice whole.
        """
        if self._reader is None:
            raise TunnelError("tunnel is not connected")
        try:
            header = await asyncio.wait_for(self._reader.readexactly(HEADER_SIZE), timeout=self._timeout)
            payload_len, _type_word, _sequence = parse_header(header)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionResetError, OSError) as exc:
            # The usual cause is that this is not a tunnel at all — see the
            # module docstring on port 6000.
            raise TunnelError(f"no tunnel answer from {self._ip}: {exc!r}") from exc
        if payload_len > MAX_FRAME_PAYLOAD:
            raise TunnelProtocolError(
                "oversized_frame", f"{self._ip} announced a {payload_len}-byte frame (limit {MAX_FRAME_PAYLOAD})"
            )
        cost = payload_len + FRAME_OVERHEAD
        if self._held + cost > TRANSFER_BUFFER_ENVELOPE:
            raise TunnelProtocolError("buffer_credit", f"{self._ip} frame does not fit beside the data held")
        # Charged before it is allocated, so nothing else can be admitted in between.
        self._held += cost
        frame = _Frame(bytearray(payload_len), 0, cost)
        try:
            payload = frame._payload
            filled = 0
            while filled < payload_len:
                chunk = await asyncio.wait_for(
                    self._reader.readexactly(min(READ_CHUNK, payload_len - filled)), timeout=self._timeout
                )
                payload[filled : filled + len(chunk)] = chunk
                filled += len(chunk)
                chunk = None
            try:
                envelope, body = split_envelope_view(payload, MAX_ENVELOPE_SCAN)
            except EnvelopeTooLong as exc:
                raise TunnelProtocolError("envelope_unbounded", f"{self._ip}: {exc}") from exc
            except ValueError:
                # ⚠️ Expected, not exceptional: the session acknowledgement is four
                # zero bytes with no JSON in it.
                logger.debug("[%s] tunnel frame with no envelope (%d bytes), skipped", self._ip, payload_len)
                self._release(frame)
                return None
            frame.start = payload_len - len(body)
            body.release()
            return envelope, frame
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionResetError, OSError) as exc:
            self._release(frame)
            raise TunnelError(f"no tunnel answer from {self._ip}: {exc!r}") from exc
        except BaseException:
            self._release(frame)
            raise
