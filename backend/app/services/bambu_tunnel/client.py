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
    pack_frame,
    parse_header,
    split_envelope,
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


class BambuTunnelClient:
    def __init__(
        self,
        ip: str,
        access_code: str,
        *,
        port: int = DEFAULT_PORT,
        timeout: float = 8.0,
        connector=None,
    ):
        self._ip = ip
        self._access_code = access_code
        self._port = port
        self._timeout = timeout
        # Tests inject a plain-TCP connector so no certificate is involved;
        # production leaves this None and we build the TLS connection here.
        self._connector = connector
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._sequence = 0
        # ⚠️ A LIST of frames per sequence, not one. A download answers a single
        # request with many frames that all carry the same sequence, and a plain
        # dict would keep only the last — silently dropping the middle of a file.
        self._parked: dict[int, list[tuple[dict, bytes]]] = {}

    async def __aenter__(self) -> BambuTunnelClient:
        await self.connect()
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
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass
        self._writer = None
        self._reader = None
        self._parked.clear()

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
        """``FILE_DOWNLOAD`` — a whole file, streamed across many frames.

        One request, then a run of replies that all carry the SAME sequence,
        each with ``offset`` / ``total`` / ``size`` in the envelope and a slice
        of the file as the body. BambuStudio accumulates until
        ``offset + size >= total`` (``PrinterFileSystem::DownloadNextFile``) and
        so do we.

        ⚠️ The shared sequence is why ``_parked`` holds a list per sequence: a
        dict would keep only the last frame and quietly lose the middle of the
        file.
        """
        sequence = self._next_sequence()
        self._send(
            TYPE_DATA_REQUEST,
            sequence,
            json.dumps(
                {"mtype": MTYPE_FILE, "cmdtype": CMD_DOWNLOAD, "sequence": sequence, "req": {"path": path}}
            ).encode(),
        )

        chunks: list[bytes] = []
        received = 0
        total: int | None = None

        while True:
            envelope, body = await self._await_sequence(sequence)
            result = envelope.get("result", 0)
            # ⚠️ ``1`` is CONTINUE, not a refusal. Only something else is.
            if result not in (0, RESULT_CONTINUE):
                raise TunnelError(f"tunnel refused the download of {path} with result={result}", result)

            reply = envelope.get("reply") or {}
            if total is None:
                total = int(reply.get("total") or 0)
            chunks.append(body)
            received += len(body)

            if total and received >= total:
                break
            if result != RESULT_CONTINUE:
                # The printer marked this frame final. Believe it, even if the
                # totals disagree — the length check below reports the mismatch.
                break

        data = b"".join(chunks)
        if total and len(data) != total:
            raise TunnelError(f"tunnel sent {len(data)} bytes of {path}, expected {total}")
        return data

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
        answer, _body = await self._await_sequence(sequence)
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
        answer, _body = await self._await_sequence(sequence)
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
        answer, body = await self._await_sequence(sequence)
        result = answer.get("result", 0)
        if result != 0:
            raise TunnelError(f"tunnel command {cmdtype} refused with result={result}", result)
        return answer.get("reply") or {}, body

    async def _await_sequence(self, sequence: int) -> tuple[dict, bytes]:
        """Read frames until the one carrying OUR sequence turns up.

        Anything else is parked rather than dropped: it belongs to a caller that
        has not asked yet, and discarding it would make the next command read a
        stale answer.
        """
        waiting = self._parked.get(sequence)
        if waiting:
            return waiting.pop(0)

        while True:
            frame = await self._read_frame()
            if frame is None:
                continue  # a bare control ack, no envelope to match
            envelope, body = frame
            found = envelope.get("sequence")
            if found == sequence:
                return envelope, body
            if isinstance(found, int):
                self._parked.setdefault(found, []).append((envelope, body))

    async def _read_frame(self) -> tuple[dict, bytes] | None:
        """One frame, or ``None`` when it carried no JSON envelope."""
        if self._reader is None:
            raise TunnelError("tunnel is not connected")
        try:
            header = await asyncio.wait_for(self._reader.readexactly(HEADER_SIZE), timeout=self._timeout)
            payload_len, _type_word, _sequence = parse_header(header)
            payload = (
                await asyncio.wait_for(self._reader.readexactly(payload_len), timeout=self._timeout)
                if payload_len
                else b""
            )
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionResetError, OSError) as exc:
            # The usual cause is that this is not a tunnel at all — see the
            # module docstring on port 6000.
            raise TunnelError(f"no tunnel answer from {self._ip}: {exc!r}") from exc

        try:
            return split_envelope(payload)
        except ValueError:
            # ⚠️ Expected, not exceptional: the session acknowledgement is four
            # zero bytes with no JSON in it.
            logger.debug("[%s] tunnel frame with no envelope (%d bytes), skipped", self._ip, len(payload))
            return None
