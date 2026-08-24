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
    with pytest.raises(ValueError):
        parse_frame(json.loads(p.read_text()))


def test_unknown_fields_ignored():
    parse_frame({"v": 1, "type": "heartbeat", "id": "a", "ts": "t", "data": {}, "future_field": True})


@pytest.mark.parametrize("p", sorted((FIX / "valid").glob("*.json")), ids=lambda p: p.stem)
def test_make_frame_round_trips(p):
    """What we serialise must be something we would accept back."""
    raw = json.loads(p.read_text())
    assert parse_frame(make_frame(parse_frame(raw))) == parse_frame(raw)
