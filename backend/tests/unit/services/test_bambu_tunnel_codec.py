"""The four traps of the Bambu file tunnel, one test each.

Every one of these was got wrong at least once during the reverse engineering,
and none of them is visible from a single successful exchange.
"""

import json

import pytest

from backend.app.services.bambu_tunnel.codec import (
    HEADER_SIZE,
    KIND_CONTROL,
    KIND_DATA,
    KIND_REFUSAL,
    TYPE_DATA_REQUEST,
    frame_kind,
    pack_frame,
    parse_header,
    split_envelope,
)


def test_header_is_sixteen_bytes_and_round_trips():
    frame = pack_frame(TYPE_DATA_REQUEST, 7, b"hello")
    assert len(frame) == HEADER_SIZE + 5
    payload_len, type_word, sequence = parse_header(frame[:HEADER_SIZE])
    assert (payload_len, type_word, sequence) == (5, TYPE_DATA_REQUEST, 7)
    assert frame[HEADER_SIZE:] == b"hello"


def test_a_short_header_is_refused():
    """The printer does not answer until it has all 16 bytes; neither do we."""
    with pytest.raises(ValueError):
        parse_header(b"\x00" * 15)


@pytest.mark.parametrize("flags", [0x00, 0x01, 0xAA, 0xB1])
def test_the_high_byte_of_type_is_opaque(flags):
    """Observed 0xaa and 0xb1 on the same kind of reply frame. It is NOT a
    last-frame marker — classifying on it would drop valid replies."""
    type_word = (flags << 24) | 0x0002013F
    assert frame_kind(type_word) == KIND_DATA


def test_kinds_are_distinguished_by_the_third_byte():
    assert frame_kind(0x0101013F) == KIND_CONTROL
    assert frame_kind(0x0102013F) == KIND_DATA
    assert frame_kind(0x0003013F) == KIND_REFUSAL


def test_a_foreign_magic_is_rejected():
    with pytest.raises(ValueError):
        frame_kind(0x01020000)


def test_envelope_and_body_share_one_frame():
    """The file bytes ride in the same frame as the envelope — there is no
    separate data frame, and the body is arbitrary binary."""
    envelope = {"cmdtype": 5, "frag_id": 0, "req": {"offset": 0, "size": 4}, "result": 1, "sequence": 3}
    payload = json.dumps(envelope).encode() + b"\n\n" + b"PK\x03\x04"
    parsed, body = split_envelope(payload)
    assert parsed["frag_id"] == 0
    assert body == b"PK\x03\x04"


def test_the_separator_is_framing_and_never_payload():
    """⚠️ BambuStudio writes ``oss << root; oss << "\\n\\n"; oss << buffer``
    (PrinterFileSystem::UploadFileTask), which is also the two bytes seen
    between `}` and the ZIP magic in every captured upload frame. Keeping them
    puts two stray bytes at the head of every file read back from the printer —
    a corruption that survives download, import and thumbnailing, and surfaces
    only when something finally opens the archive."""
    payload = json.dumps({"sequence": 1}).encode() + b"\n\n" + b"PK\x03\x04body"
    _parsed, body = split_envelope(payload)
    assert body == b"PK\x03\x04body"
    assert not body.startswith(b"\n")


def test_only_one_separator_is_eaten():
    """A body that genuinely begins with a newline keeps it — exactly one
    separator belongs to the framing."""
    payload = json.dumps({"sequence": 1}).encode() + b"\n\n" + b"\ntext"
    _parsed, body = split_envelope(payload)
    assert body == b"\ntext"


def test_a_body_with_no_separator_is_still_read():
    """Not every frame carries one; the split must not depend on it."""
    payload = json.dumps({"sequence": 1}).encode() + b"PK\x03\x04"
    _parsed, body = split_envelope(payload)
    assert body == b"PK\x03\x04"


def test_envelope_split_survives_braces_inside_strings():
    payload = json.dumps({"path": "/a{b}c.3mf", "sequence": 1}).encode() + b"\xff\xfe"
    parsed, body = split_envelope(payload)
    assert parsed["path"] == "/a{b}c.3mf"
    assert body == b"\xff\xfe"


def test_envelope_split_survives_an_escaped_quote():
    payload = json.dumps({"path": 'he said "hi"', "sequence": 1}).encode() + b"\x00"
    parsed, body = split_envelope(payload)
    assert parsed["path"] == 'he said "hi"'
    assert body == b"\x00"


def test_envelope_split_survives_an_escaped_backslash_before_a_quote():
    r"""A path ending in a backslash: the escape must not swallow the quote that
    closes the string, or the scan runs past the envelope and into the body."""
    payload = json.dumps({"path": "C:\\", "sequence": 1}).encode() + b"\x01\x02"
    parsed, body = split_envelope(payload)
    assert parsed["path"] == "C:\\"
    assert body == b"\x01\x02"


def test_envelope_split_survives_a_nested_object():
    payload = json.dumps({"req": {"paths": ["/a.3mf"]}, "sequence": 1}).encode() + b"\xde\xad"
    parsed, body = split_envelope(payload)
    assert parsed["req"]["paths"] == ["/a.3mf"]
    assert body == b"\xde\xad"


def test_an_envelope_with_no_body_yields_empty_bytes():
    payload = json.dumps({"sequence": 2}).encode()
    parsed, body = split_envelope(payload)
    assert parsed == {"sequence": 2}
    assert body == b""


def test_a_payload_with_no_complete_envelope_is_refused():
    with pytest.raises(ValueError):
        split_envelope(b'{"sequence": 2')
