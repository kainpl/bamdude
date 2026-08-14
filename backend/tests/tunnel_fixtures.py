"""A Bambu file tunnel that answers the way the real one does — including the
parts that are inconvenient.

Deliberately awkward on purpose. A fake that replied in order, with a tidy
``0x00`` flag byte and nothing but JSON, would confirm exactly the model of the
protocol we already know is wrong. This one:

* sends a bare 4-byte control acknowledgement after the session frame, with no
  JSON in it at all — the live X2D does this and a client that assumes every
  frame carries an envelope dies on the first one;
* answers out of order when asked to, because the live X2D's reply to the
  session frame arrived *after* the next request had gone out;
* stamps a non-zero flag byte on data replies, because ``0xaa`` and ``0xb1``
  were both observed there and neither means "last frame".

Named after ``zigbee_fixtures.py`` next door — shared test scaffolding lives
flat in ``backend/tests/``, not in a package of its own.
"""

from __future__ import annotations

import asyncio
import json

from backend.app.services.bambu_tunnel.codec import (
    ENVELOPE_SEPARATOR,
    HEADER_SIZE,
    pack_frame,
    parse_header,
    split_envelope,
)

_TYPE_CONTROL_REPLY = 0x0001013F
# A non-zero flag byte, as the real printer sends. Anything that classifies on
# the high byte will fail against this fake, which is the point.
_TYPE_DATA_REPLY = 0xB102013F

MTYPE_SESSION = 12291
MTYPE_FILE = 12289


class FakeTunnelServer:
    """A printer-shaped peer for the tunnel client.

    Attributes worth setting in a test:
        files: the entries ``cmdtype 1`` will return.
        file_bytes: path -> bytes that ``cmdtype 2`` will return.
        answer_out_of_order: hold the first reply back so the second overtakes it.
        report_start: what the listing reply claims in ``start``.
        fail_next_with: make the next command answer a non-zero ``result``.

    Attributes worth reading after one:
        requests: every envelope the client sent, in order.
        deleted: paths passed to ``cmdtype 3``.
    """

    def __init__(self, access_code: str = "12345678"):
        self.access_code = access_code
        self.requests: list[dict] = []
        self.files: list[dict] = []
        self.file_bytes: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.answer_out_of_order = False
        # Real captures always showed 0. A non-zero value exists only so the
        # client can be tested for warning about a listing it cannot continue.
        self.report_start = 0
        # Small on purpose: every download of a non-trivial file must span
        # several frames in the tests.
        self.download_chunk_size = 64
        self.fail_next_with: int | None = None
        self.auth_rejected = False
        # Uploads, reassembled from their fragments.
        self.uploads: dict[str, bytes] = {}
        self.upload_meta: dict[str, dict] = {}
        # Every fragment envelope in order, so a test can assert on frag_id and
        # on the result flag that means "more to come".
        self.upload_frames: list[dict] = []
        self._upload_open: dict[int, str] = {}
        self._pending_body = b""
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        host, port = self._server.sockets[0].getsockname()[:2]
        return host, port

    async def stop(self) -> None:
        """Hang up on everyone, then close.

        ⚠️ Python 3.12's ``Server.wait_closed()`` waits for every active handler
        to finish, so a test whose client is still connected — including one
        that failed an assertion before reaching its ``close()`` — would block
        here forever. Closing the peers first makes ``stop()`` safe no matter
        how the test ended, which is the difference between a failing test and
        a hung suite.
        """
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _download_frames(self, envelope: dict, sequence: int) -> list[bytes]:
        """FILE_DOWNLOAD: one request, a run of frames sharing one sequence.

        ⚠️ Deliberately more than one frame whenever the file allows it. A fake
        that answered a download in a single frame would let a client that
        keeps only the last reply per sequence look correct while losing the
        middle of every real file.
        """
        path = (envelope.get("req") or {}).get("path", "")
        data = self.file_bytes.get(path)
        if data is None:
            return [self._data({"cmdtype": 4, "mtype": MTYPE_FILE, "sequence": sequence, "result": 14, "reply": {}})]

        total = len(data)
        frames: list[bytes] = []
        offset = 0
        step = max(1, self.download_chunk_size)
        while offset < total or total == 0:
            piece = data[offset : offset + step]
            is_last = offset + len(piece) >= total
            out = {
                "cmdtype": 4,
                "mtype": MTYPE_FILE,
                "sequence": sequence,
                # ⚠️ CONTINUE (1) on every frame but the last, mirroring the
                # live printer. A fake that answered 0 throughout would let a
                # client treating non-zero as a refusal pass — which is exactly
                # how every real download failed on its first frame.
                "result": 0 if is_last else 1,
                "reply": {"offset": offset, "total": total, "size": len(piece)},
            }
            frames.append(pack_frame(_TYPE_DATA_REPLY, sequence, json.dumps(out).encode() + ENVELOPE_SEPARATOR + piece))
            offset += len(piece)
            if total == 0:
                break
        return frames

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            auth = await self._read_frame(reader)
            if auth is None or not self._auth_ok(auth[2]):
                self.auth_rejected = True
                return

            held: bytes | None = None
            # ⚠️ The live printer lagged ONCE, on the session frame — it did not
            # adopt reordering as a policy. A fake that holds every first reply
            # forever never flushes its last one and hangs the client, which
            # says nothing about the client.
            may_hold = self.answer_out_of_order
            while True:
                frame = await self._read_frame(reader)
                if frame is None:
                    return
                _type_word, _sequence, payload = frame
                try:
                    envelope, body = split_envelope(payload)
                except ValueError:
                    continue
                # ⚠️ The bytes ride in the same frame as the envelope, so the
                # reply logic needs them alongside it.
                self._pending_body = body
                self.requests.append(envelope)

                if envelope.get("mtype") == MTYPE_SESSION:
                    # The bare control ack the live printer sends: four zero
                    # bytes, no JSON. Its own JSON reply comes later, below.
                    writer.write(pack_frame(_TYPE_CONTROL_REPLY, 0, b"\x00\x00\x00\x00"))
                    await writer.drain()

                if envelope.get("cmdtype") == 4:
                    for frame in self._download_frames(envelope, envelope.get("sequence", 0)):
                        writer.write(frame)
                    await writer.drain()
                    continue

                reply = self._reply_for(envelope)
                if reply is None:
                    continue

                if may_hold:
                    may_hold = False
                    held = reply
                    continue
                writer.write(reply)
                if held is not None:
                    writer.write(held)
                    held = None
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            self._writers.discard(writer)
            writer.close()

    def _auth_ok(self, payload: bytes) -> bool:
        return (
            len(payload) == 16 and payload[:4] == b"bblp" and payload[8:].decode(errors="replace") == self.access_code
        )

    async def _read_frame(self, reader: asyncio.StreamReader):
        try:
            header = await reader.readexactly(HEADER_SIZE)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return None
        payload_len, type_word, sequence = parse_header(header)
        try:
            payload = await reader.readexactly(payload_len) if payload_len else b""
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return None
        return type_word, sequence, payload

    def _reply_for(self, envelope: dict) -> bytes | None:
        sequence = envelope.get("sequence", 0)
        mtype = envelope.get("mtype")

        # ⚠️ Only a real command fails — never the session frame. Letting the
        # session consume it would leave the command under test succeeding,
        # and the test would pass or fail for the wrong reason.
        if self.fail_next_with is not None and mtype == MTYPE_FILE:
            result, self.fail_next_with = self.fail_next_with, None
            return self._data({"mtype": mtype, "sequence": sequence, "result": result, "reply": {}})

        if mtype == MTYPE_SESSION:
            return self._data({"mtype": MTYPE_SESSION, "sequence": sequence, "result": 0, "reply": {}})

        cmdtype = envelope.get("cmdtype")
        if cmdtype == 5:
            return self._reply_for_upload(envelope, sequence)
        if cmdtype == 7:
            return self._data(
                {
                    "cmdtype": 7,
                    "mtype": MTYPE_FILE,
                    "sequence": sequence,
                    "result": 0,
                    "reply": {
                        "storage": ["emmc", "udisk"],
                        "upload_storage": ["emmc", "udisk"],
                        "allow_internal_model_download": True,
                        "api_version": 3,
                    },
                }
            )
        if cmdtype == 1:
            return self._data(
                {
                    "cmdtype": 1,
                    "mtype": MTYPE_FILE,
                    "sequence": sequence,
                    "result": 0,
                    "reply": {
                        "dir_refresh_cnt": 0,
                        "file_lists": list(self.files),
                        "start": self.report_start,
                    },
                }
            )
        if cmdtype == 2:
            # ⚠️ SUB_FILE reads a MEMBER of a container. The real printer
            # answers result=14 for a plain path, and a fake that served whole
            # files here is exactly what let a broken client pass every test.
            path = (envelope.get("req") or {}).get("paths", [""])[0]
            if "#" not in path:
                return self._data({"cmdtype": 2, "mtype": MTYPE_FILE, "sequence": sequence, "result": 14, "reply": {}})
            body = self.file_bytes.get(path, b"")
            out = {"cmdtype": 2, "mtype": MTYPE_FILE, "sequence": sequence, "result": 0, "reply": {}}
            # ⚠️ The separator BambuStudio writes between envelope and bytes.
            # A fake that omitted it would let a client which treats those two
            # bytes as payload pass every test and corrupt every real file.
            return pack_frame(
                _TYPE_DATA_REPLY,
                sequence,
                json.dumps(out).encode() + ENVELOPE_SEPARATOR + body,
            )
        if cmdtype == 3:
            self.deleted.extend((envelope.get("req") or {}).get("paths", []))
            return self._data({"cmdtype": 3, "mtype": MTYPE_FILE, "sequence": sequence, "result": 0, "reply": {}})
        return None

    def _reply_for_upload(self, envelope: dict, sequence: int) -> bytes | None:
        """The three phases of ``cmdtype 5``, answered the way the printer does.

        ⚠️ Only the open and the LAST fragment get a reply. ``result`` in the
        request means "more to come", so a fragment carrying ``1`` is
        acknowledged by silence — a fake that answered every fragment would let
        a client that awaits per-fragment look correct.
        """
        req = envelope.get("req") or {}

        if "path" in req:
            self._upload_open[sequence] = req["path"]
            self.uploads[req["path"]] = b""
            self.upload_meta[req["path"]] = dict(req)
            return self._data({"cmdtype": 5, "mtype": MTYPE_FILE, "sequence": sequence, "result": 0, "reply": {}})

        self.upload_frames.append(envelope)
        path = self._upload_open.get(sequence)
        if path is None:
            # A fragment for a transfer that was never opened.
            return self._data({"cmdtype": 5, "mtype": MTYPE_FILE, "sequence": sequence, "result": 1, "reply": {}})

        self.uploads[path] += self._pending_body
        if envelope.get("result") == 0:
            self.upload_meta[path]["file_md5"] = req.get("file_md5")
            return self._data({"cmdtype": 5, "mtype": MTYPE_FILE, "sequence": sequence, "result": 0, "reply": {}})
        return None

    def _data(self, envelope: dict) -> bytes:
        return pack_frame(_TYPE_DATA_REPLY, envelope["sequence"], json.dumps(envelope).encode())


def listing_entry(name: str, size: int = 10, time: int = 1786638756) -> dict:
    """One ``file_lists`` element, in the shape the live X2D returned."""
    return {
        "duration_ms": 0,
        "model_name": "",
        "name": name,
        "path": f"/userdata/model/history/{name}",
        "size": size,
        "time": time,
    }
