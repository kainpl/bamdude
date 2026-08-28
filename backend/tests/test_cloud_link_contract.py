"""Golden-fixture tests for the Cloud Link envelope v1 contract.

The wire contract is defined once, as zod schemas in the portal repo, and
exported from there as the fixture snapshot under `fixtures/cloud_link/v1`.
This module is the only thing that keeps the pydantic mirror honest: every
valid fixture must parse, every invalid one must be refused, and an unknown
field must be ignored rather than rejected (the forever-rule that lets the
portal add fields without stranding old agents).

Fixtures are copied, never edited. A test failing here means the mirror
drifted from the contract -- fix `schemas.py`, or re-snapshot if the
contract itself moved.
"""

import json
from pathlib import Path

import pytest

from backend.app.services.cloud_link.schemas import make_frame, parse_frame

FIX = Path(__file__).parent / "fixtures" / "cloud_link" / "v1"


@pytest.mark.parametrize("p", sorted((FIX / "valid").glob("*.json")), ids=lambda p: p.stem)
def test_valid_fixtures_parse(p):
    parse_frame(json.loads(p.read_text()))


@pytest.mark.parametrize("p", sorted((FIX / "invalid").glob("*.json")), ids=lambda p: p.stem)
def test_invalid_fixtures_rejected(p):
    with pytest.raises(ValueError) as excinfo:
        parse_frame(json.loads(p.read_text()))
    # pydantic's ValidationError IS a ValueError, so `pytest.raises(ValueError)`
    # alone would pass even if the conversion in parse_frame were deleted.
    assert type(excinfo.value) is ValueError


def test_unknown_fields_ignored():
    parse_frame({"v": 1, "type": "heartbeat", "id": "a", "ts": "t", "data": {}, "future_field": True})


def _numbers_as_floats(value):
    """Normalise 42 and 42.0 to the same thing.

    JSON has one number type; Python has two. The models declare ``float``, so a
    fixture's ``42`` comes back as ``42.0`` — identical on the wire, unequal in
    Python. Nothing else may differ.
    """
    if isinstance(value, bool):  # bool is an int subclass — check it first
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return {k: _numbers_as_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_numbers_as_floats(v) for v in value]
    return value


@pytest.mark.parametrize("p", sorted((FIX / "valid").glob("*.json")), ids=lambda p: p.stem)
def test_make_frame_reproduces_the_fixture(p):
    """What we emit must be what the contract's own example looks like.

    Compared against the RAW fixture, not against a re-parse of our own output:
    a mirror agreeing with itself proves nothing. This is what catches a field
    that zod declares ``.optional()`` (absent when unset) being emitted as an
    explicit ``null``, which zod refuses.
    """
    raw = json.loads(p.read_text())
    assert _numbers_as_floats(make_frame(parse_frame(raw))) == _numbers_as_floats(raw)


def test_fixture_snapshot_is_present_and_current():
    """A parametrize over an empty glob passes with zero cases — guard that."""
    cases = list((FIX / "valid").glob("*.json")) + list((FIX / "invalid").glob("*.json"))
    assert len(cases) >= 15, f"fixture snapshot looks truncated: {len(cases)} case(s)"
    assert (FIX / "VERSION").read_text().strip() == "envelope-v1"
