"""Frames of the Bambu file tunnel (TCP 6000).

A frame is a 16-byte header followed by exactly ``payload_len`` bytes::

    <I payload_len> <I type> <I sequence> <I 0>   +   payload

``type`` decomposes as ``3f 01 | kind | flags`` little-endian. The low two
bytes are the protocol magic, the third byte is the kind, and the fourth is
**opaque**.

⚠️ **The fourth byte is not a direction and not a last-frame marker.** Requests
carry ``0x01`` and replies ``0x00``, but ``0xaa`` and ``0xb1`` were both
observed on ordinary listing replies. Classify on the kind byte and ignore the
rest, or valid replies get dropped.

⚠️ **The JSON envelope and the file bytes share one frame**, separated by
``\\n\\n``. The split is done by scanning for the balanced brace — the body is
arbitrary binary and may not be valid UTF-8, which rules out decoding the
payload as text first — and then stepping over the separator.

The separator is not a guess: BambuStudio's own open-source
``PrinterFileSystem::UploadFileTask`` writes ``oss << root; oss << "\\n\\n";
oss << buffer``, and it accounts for the two bytes seen between ``}`` and the
ZIP magic in every captured upload frame. Treating it as payload puts two
stray bytes at the front of every file read back from the printer.
"""

from __future__ import annotations

import json
import struct

HEADER = struct.Struct("<IIII")
HEADER_SIZE = HEADER.size  # 16

_MAGIC = 0x013F

KIND_CONTROL = 1
KIND_DATA = 2
KIND_REFUSAL = 3

TYPE_CONTROL_REQUEST = 0x0101013F
TYPE_DATA_REQUEST = 0x0102013F

_QUOTE = 0x22
_BACKSLASH = 0x5C
_OPEN_BRACE = 0x7B
_CLOSE_BRACE = 0x7D

# What separates the envelope from the bytes riding with it. BambuStudio writes
# it literally (``oss << "\n\n"``), so it belongs to the framing and never to
# the payload.
ENVELOPE_SEPARATOR = b"\n\n"


def pack_frame(type_word: int, sequence: int, body: bytes) -> bytes:
    """A complete frame: 16-byte header plus the payload it announces."""
    return HEADER.pack(len(body), type_word, sequence, 0) + body


def parse_header(raw: bytes) -> tuple[int, int, int]:
    """``(payload_len, type_word, sequence)`` from exactly 16 bytes."""
    if len(raw) != HEADER_SIZE:
        raise ValueError(f"tunnel header must be {HEADER_SIZE} bytes, got {len(raw)}")
    payload_len, type_word, sequence, _reserved = HEADER.unpack(raw)
    return payload_len, type_word, sequence


def frame_kind(type_word: int) -> int:
    """Control / data / refusal, with the flag byte deliberately ignored."""
    if (type_word & 0xFFFF) != _MAGIC:
        raise ValueError(f"not a tunnel frame type: {type_word:#010x}")
    return (type_word >> 16) & 0xFF


def split_envelope(payload: bytes, max_scan: int | None = None) -> tuple[dict, bytes]:
    """Split a payload into its JSON envelope and the binary tail (a copy of it).

    See :func:`split_envelope_view`, which this wraps for callers that want bytes.
    """
    envelope, body = split_envelope_view(payload, max_scan)
    return envelope, bytes(body)


class EnvelopeTooLong(ValueError):
    """A payload starts like an envelope but its JSON does not close within the scan bound."""


def split_envelope_view(payload: bytes, max_scan: int | None = None) -> tuple[dict, memoryview]:
    """Split a payload into its JSON envelope and a VIEW of the binary tail — no copy of the bytes.

    Scans bytes rather than decoding, because the tail is arbitrary binary and
    is routinely not valid UTF-8 (it is usually the head of a ZIP).

    ``max_scan`` bounds the scan (G10 CR5): the envelope is a few hundred bytes,
    and scanning a whole 8 MiB frame of binary for a brace that never comes cost
    most of a second of the event loop (U0 probe T5). A payload that does not
    even start with ``{`` has no envelope at all and is not scanned.

    ⚠️ A single ``\\n\\n`` after the envelope is framing and is dropped. Keeping
    it would put two stray bytes at the front of every file read back from the
    printer — a corruption that survives a download, an import and a thumbnail,
    and only shows up when something finally tries to open the archive.
    """
    if not payload or payload[:1] != b"{":
        raise ValueError("tunnel payload has no JSON envelope")
    depth = 0
    in_string = False
    escaped = False
    end = -1
    limit = len(payload) if max_scan is None else min(len(payload), max_scan)
    view = memoryview(payload)

    for index in range(limit):
        byte = view[index]
        if in_string:
            if escaped:
                escaped = False
            elif byte == _BACKSLASH:
                escaped = True
            elif byte == _QUOTE:
                in_string = False
            continue
        if byte == _QUOTE:
            in_string = True
        elif byte == _OPEN_BRACE:
            depth += 1
        elif byte == _CLOSE_BRACE:
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    if end < 0:
        if limit < len(payload):
            raise EnvelopeTooLong(f"tunnel envelope does not close within {limit} bytes")
        raise ValueError("tunnel payload has no complete JSON envelope")

    body = view[end:]
    if body[: len(ENVELOPE_SEPARATOR)] == ENVELOPE_SEPARATOR:
        body = body[len(ENVELOPE_SEPARATOR) :]

    return json.loads(bytes(view[:end]).decode("utf-8")), body
