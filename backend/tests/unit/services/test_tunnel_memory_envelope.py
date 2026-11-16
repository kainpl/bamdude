"""The tunnel client's Python buffers stay inside one envelope per active file (G10 A, Codex review A4).

Spec r4 §12: at most 8 MiB of Python transfer buffers per active file — the
payload being read and what reading and parsing it costs, the parked frames,
and a copy handed out, all charged together, with a refusal decided from the
header before anything is allocated.

The wire is built before ``tracemalloc`` starts, so what is measured is the
client: its payload buffers, the chunks it reads (copies, as from a real
``StreamReader``), its parsed envelopes and its copies.
"""

from __future__ import annotations

import asyncio
import json
import threading
import tracemalloc
from types import SimpleNamespace

import pytest

from backend.app.services.bambu_tunnel import client as tc
from backend.app.services.bambu_tunnel.client import BambuTunnelClient, TunnelProtocolError
from backend.app.services.bambu_tunnel.codec import ENVELOPE_SEPARATOR, TYPE_DATA_REQUEST, pack_frame
from backend.app.services.printer_file_lanes import PrinterFileRuntime
from backend.app.services.printer_files import tunnel as tunnel_transport
from backend.app.services.printer_files.base import TRANSFER_COMPLETED
from backend.app.services.printer_files.tunnel import TunnelTransport
from backend.tests.tunnel_fixtures import FakeTunnelServer

MIB = 1024 * 1024
ENVELOPE = tc.TRANSFER_BUFFER_ENVELOPE
MAX_FRAME = tc.MAX_FRAME_PAYLOAD


def _envelope(sequence: int, *, reply: dict | None = None, result: int = 0, pad: int = 0) -> bytes:
    data: dict = {"sequence": sequence, "result": result, "reply": reply or {}}
    if pad:
        # The worst shape measured for objects per byte of JSON: empty lists.
        data["pad"] = [[] for _ in range(pad)]
    return json.dumps(data, separators=(",", ":")).encode()


def _frame(sequence: int, body: bytes = b"", **envelope) -> bytes:
    return pack_frame(TYPE_DATA_REQUEST, sequence, _envelope(sequence, **envelope) + ENVELOPE_SEPARATOR + body)


def _largest_body(sequence: int, **envelope) -> int:
    """The body that makes this frame exactly the largest one admitted."""
    return MAX_FRAME - len(_envelope(sequence, **envelope)) - len(ENVELOPE_SEPARATOR)


class Reader:
    """``readexactly`` over a prebuilt wire; each call returns a copy, as a StreamReader does."""

    def __init__(self, raw: bytes):
        self.raw, self.pos = raw, 0

    async def readexactly(self, n: int) -> bytes:
        if self.pos + n > len(self.raw):
            raise asyncio.IncompleteReadError(self.raw[self.pos :], n)
        block = self.raw[self.pos : self.pos + n]
        self.pos += n
        return block


class Writer:
    def write(self, data) -> None:
        pass

    async def drain(self) -> None:
        pass


def _nested(depth: int) -> str:
    leaf = "{}"
    for _ in range(depth):
        leaf = '{"":' + leaf + "}"
    return leaf


#: Units an envelope is padded with, up to the scan bound: each makes many Python
#: objects per byte of JSON. ``nested_dict`` is the shape of Codex's re-review probe.
SHAPES = {
    "empty_lists": "[]",
    "empty_dicts": "{}",
    "nested_dict": _nested(20),
    "deep_lists": "[" * 30 + "]" * 30,
}


def _full_envelope(sequence: int, shape: str, *, reply: dict | None = None, result: int = 0) -> bytes:
    """An envelope just under the scan bound, padded with ``shape`` — valid JSON the client accepts."""
    head = json.dumps({"sequence": sequence, "result": result, "reply": reply or {}}, separators=(",", ":"))
    prefix = head[:-1] + ',"pad":['
    unit = SHAPES[shape]
    count = (tc.MAX_ENVELOPE_SCAN - len(prefix) - 16) // (len(unit) + 1)
    envelope = (prefix + ",".join([unit] * count) + "]}").encode()
    assert len(envelope) < tc.MAX_ENVELOPE_SCAN
    return envelope


def _shaped_frame(sequence: int, payload: int, shape: str | None, **envelope) -> bytes:
    """A frame of exactly ``payload`` bytes whose envelope is padded with ``shape`` (None: a small one)."""
    head = _full_envelope(sequence, shape, **envelope) if shape else _envelope(sequence, **envelope)
    body = b"x" * (payload - len(head) - len(ENVELOPE_SEPARATOR))
    return pack_frame(TYPE_DATA_REQUEST, sequence, head + ENVELOPE_SEPARATOR + body)


def _client(raw: bytes) -> BambuTunnelClient:
    client = BambuTunnelClient("no-network", "fake")
    client._reader = Reader(raw)
    client._writer = Writer()
    return client


async def _measured(coro):
    tracemalloc.start()
    try:
        try:
            outcome = await coro
        except TunnelProtocolError as exc:
            outcome = exc
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return outcome, peak


@pytest.mark.asyncio
class TestOneEnvelope:
    async def test_parked_frames_and_the_awaited_one_share_the_envelope(self):
        raw = _frame(2, b"a" * _largest_body(2)) + _frame(3, b"b" * _largest_body(3)) + _frame(1, b"z")
        client = _client(raw)
        (envelope, frame), peak = await _measured(client._await_sequence(1))
        assert bytes(frame.body()) == b"z"
        assert client._parked_count == 2
        assert client._parked_bytes == 2 * MAX_FRAME
        assert client._held <= ENVELOPE
        assert peak <= ENVELOPE

    async def test_a_frame_that_does_not_fit_is_refused_before_it_is_allocated(self):
        raw = (
            _frame(2, b"a" * _largest_body(2))
            + _frame(3, b"b" * _largest_body(3))
            + _frame(4, b"c" * _largest_body(4))
            + _frame(1, b"z")
        )
        client = _client(raw)
        refused, peak = await _measured(client._await_sequence(1))
        assert isinstance(refused, TunnelProtocolError) and refused.category == "buffer_credit"
        # Two parked frames and one read chunk at most: the third payload was never allocated.
        assert peak < 2 * MAX_FRAME + MIB

    async def test_the_reviewed_six_mib_foreign_frames_are_refused_from_their_header(self):
        """Codex's probe: two 6 MiB foreign frames used to be parked (12 MiB held, 18.5 MiB peak)."""
        raw = _frame(2, b"x" * (6 * MIB)) + _frame(3, b"y" * (6 * MIB)) + _frame(1, b"z")
        client = _client(raw)
        refused, peak = await _measured(client._await_sequence(1))
        assert isinstance(refused, TunnelProtocolError) and refused.category == "oversized_frame"
        assert peak < MIB

    async def test_a_streamed_download_never_holds_two_frames(self):
        count = 8
        # Sized against wider numbers than the real ones, so every frame stays within the cap.
        widest = {"offset": 99_999_999, "total": 99_999_999, "size": 9_999_999}
        sizes = [_largest_body(1, reply=widest, result=1)] * count
        frames, total, offset = [], sum(sizes), 0
        for index, size in enumerate(sizes):
            reply = {"offset": offset, "total": total, "size": size}
            frames.append(_frame(1, b"d" * size, reply=reply, result=0 if index == count - 1 else 1))
            offset += size
        client = _client(b"".join(frames))
        held_in_sink: list[int] = []

        async def sink(chunk) -> None:
            held_in_sink.append(client._held)

        receipt, peak = await _measured(client.download_into("/f", sink))
        assert receipt.total == total
        assert max(held_in_sink) <= MAX_FRAME + tc.FRAME_OVERHEAD
        # Each frame is released before the next is admitted: never two payloads at once.
        assert peak < 2 * MAX_FRAME
        assert client._held == 0

    async def test_a_reply_and_its_copy_fit_together(self):
        raw = _frame(1, b"p" * _largest_body(1))
        client = _client(raw)
        data, peak = await _measured(client.read_sub_file("/a.3mf#Metadata/plate_1.png", "internal"))
        assert len(data) == _largest_body(1)
        assert peak <= ENVELOPE
        assert client._held == 0

    async def test_the_worst_envelope_fits_its_overhead_live_parked_and_claimed(self):
        pad = 20_000  # ~60 KiB of JSON: empty lists, the most objects per byte
        assert len(_envelope(2, pad=pad)) < tc.MAX_ENVELOPE_SCAN
        raw = (
            _frame(3, b"a" * _largest_body(3))
            + _frame(2, b"w", pad=pad)
            + _frame(1, b"p" * _largest_body(1, pad=pad), pad=pad)
        )
        client = _client(raw)

        async def scenario():
            # The awaited frame arrives last, behind a full frame and a worst envelope, both parked.
            _env, mine = await client._await_sequence(1)
            client._release(mine)
            # The parked worst envelope is parsed again when claimed.
            env, worst = await client._await_sequence(2)
            assert len(env["pad"]) == pad
            client._release(worst)

        _none, peak = await _measured(scenario())
        assert peak <= ENVELOPE
        assert client._held == tc.MAX_FRAME_PAYLOAD  # only the parked full frame is left

    async def test_a_parked_frame_keeps_no_envelope_objects(self):
        pad = 20_000
        raw = _frame(2, b"w", pad=pad) + _frame(1, b"z")
        client = _client(raw)
        _env, frame = await client._await_sequence(1)
        client._release(frame)
        (parked,) = client._parked[2]
        # Charged exactly its payload: its parsed envelope was dropped.
        assert parked.charge == parked.payload_len


@pytest.mark.asyncio
class TestNoUnchargedEnvelopeOutlivesItsCharge:
    """Codex re-review A4: a parsed envelope given back to the budget must be gone, not merely uncharged.

    The charge is exactly the envelope here — 3 MiB parked, then a 2 MiB live
    frame with its overhead — so any envelope still alive past its charge
    shows up as a peak above it.
    """

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    async def test_a_parked_frames_envelope_is_gone_before_the_next_is_parsed(self, shape):
        raw = _shaped_frame(2, 2 * MIB, None) + _shaped_frame(3, MIB, shape) + _shaped_frame(1, MAX_FRAME, shape)
        client = _client(raw)
        (envelope, frame), peak = await _measured(client._await_sequence(1))
        assert client._held == ENVELOPE  # the scenario sits exactly at the bound
        assert peak <= ENVELOPE, peak
        client._release(frame)

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    async def test_an_upload_drops_the_open_answer_before_reading_its_completion(self, shape, tmp_path):
        local = tmp_path / "u.3mf"
        local.write_bytes(b"u" * 512)
        raw = (
            _shaped_frame(1, 128 * 1024, shape, reply={"chunk_size": 1, "offset": 0}, result=1)
            + _shaped_frame(7, 2 * MIB, None)
            + _shaped_frame(8, MIB, shape)
            + _shaped_frame(1, MAX_FRAME, shape)
        )
        client = _client(raw)
        _none, peak = await _measured(client.upload_file(local, "u.3mf", "emmc"))
        assert peak <= ENVELOPE, peak

    async def test_an_envelope_nested_too_deep_to_parse_is_a_typed_refusal(self):
        payload = b'{"sequence":1,"x":' + b"[" * 20_000 + b"]" * 20_000 + b"}"
        client = _client(pack_frame(TYPE_DATA_REQUEST, 1, payload))
        refused, _peak = await _measured(client._await_sequence(1))
        assert isinstance(refused, TunnelProtocolError) and refused.category == "envelope_unbounded"
        assert client._held == 0


@pytest.fixture
async def blocked_sink(monkeypatch, tmp_path):
    server = FakeTunnelServer()
    host, port = await server.start()
    runtime = PrinterFileRuntime()
    monkeypatch.setattr(tunnel_transport, "printer_file_runtime", runtime)
    readers: list[asyncio.StreamReader] = []

    async def connector():
        reader, writer = await asyncio.open_connection(host, port)
        readers.append(reader)
        return reader, writer

    printer = SimpleNamespace(ip_address="127.0.0.1", access_code=server.access_code)
    transport = TunnelTransport(printer, port=port, connector=connector)
    yield SimpleNamespace(server=server, transport=transport, readers=readers, tmp=tmp_path)
    await server.stop()


@pytest.mark.asyncio
async def test_a_blocked_disk_write_holds_one_frame_and_a_bounded_stream(blocked_sink, monkeypatch):
    """While the disk write blocks, the client reads nothing more: one frame, and the stream's own bound."""
    b = blocked_sink
    path = "/userdata/media/timelapse/big.mp4"
    b.server.file_bytes[path] = b"q" * (16 * MIB)
    b.server.download_chunk_size = MAX_FRAME - 1024
    held: list[int] = []
    buffered: list[int] = []
    gate = threading.Event()
    real_open = open
    original_client = TunnelTransport._client

    def watched(self):
        client = original_client(self)
        watched.client = client
        return client

    class SlowFile:
        def __init__(self, real):
            self._real, self.calls = real, 0

        def write(self, data):
            self.calls += 1
            if self.calls == 1:
                assert gate.wait(10)
            return self._real.write(data)

        def flush(self):
            return self._real.flush()

        def close(self):
            return self._real.close()

        @property
        def closed(self):
            return self._real.closed

    monkeypatch.setattr(TunnelTransport, "_client", watched)
    monkeypatch.setattr(
        tunnel_transport, "open", lambda p, m="r", *a, **k: SlowFile(real_open(p, m, *a, **k)), raising=False
    )
    task = asyncio.create_task(b.transport.download_to_file(path, b.tmp / "big.mp4"))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.0
    while loop.time() < deadline:
        await asyncio.sleep(0.01)
        if b.readers:
            held.append(watched.client._held)
            buffered.append(len(b.readers[0]._buffer))
    gate.set()
    outcome = await task
    assert outcome.status == TRANSFER_COMPLETED
    assert max(held) <= MAX_FRAME + tc.FRAME_OVERHEAD
    # The stream stops reading at its own limit while nobody reads it: far under the overhead.
    assert max(buffered) <= MIB
