"""N07 spec §7 T1: the claim, the mapping, ``exp`` and the evidence are read strictly."""

from __future__ import annotations

import math

import pytest

from backend.app.services import oidc_group_sync as sync


@pytest.mark.parametrize(
    "claims,expected",
    [
        ({"groups": ["a", "b"]}, ["a", "b"]),
        ({"groups": "one group with spaces"}, ["one group with spaces"]),  # one string is ONE group
        ({}, []),
        ({"groups": None}, []),
        ({"groups": ["  padded  ", "a", "a"]}, ["a", "padded"]),
        ({"groups": ["Team", "team"]}, ["Team", "team"]),  # case is kept
        ({"groups": ["", "  "]}, []),
    ],
)
def test_supported_shapes(claims, expected):
    assert sync.idp_groups(claims, "groups") == expected


@pytest.mark.parametrize(
    "claims",
    [
        {"hasgroups": True},
        {"hasgroups": True, "groups": None},
        {"hasgroups": True, "groups": ["a"]},  # the marker wins over a present array
        {"_claim_names": {"groups": "src1"}},
        {"_claim_names": {"groups": "src1"}, "groups": ["a"]},
    ],
)
def test_incompleteness_markers_win(claims):
    with pytest.raises(sync.CallbackRefused) as refused:
        sync.idp_groups(claims, "groups")
    assert refused.value.code == sync.CLAIMS_INCOMPLETE


@pytest.mark.parametrize(
    "claims",
    [
        {"groups": {"a": 1}},
        {"groups": 5},
        {"groups": ["a", 1]},
        {"groups": ["a"] * (sync.MAX_CLAIM_GROUPS + 1)},
        {"_claim_names": "groups"},
        {"_claim_names": None},  # present as JSON null: not "no marker" (N07-A01)
        {"_claim_names": None, "groups": []},
        {"_claim_names": None, "groups": ["a"]},
    ],
)
def test_invalid_values_are_refused(claims):
    with pytest.raises(sync.CallbackRefused) as refused:
        sync.idp_groups(claims, "groups")
    assert refused.value.code == sync.CLAIMS_INVALID


def test_other_claim_names_are_exact():
    assert sync.idp_groups({"Groups": ["a"], "groups": ["b"]}, "Groups") == ["a"]


# ── mapping (spec §F, S16) ───────────────────────────────────────────────────


def test_the_normalizer_trims_and_keeps_strict_ids():
    assert sync.normalize_mapping({" team ": 3, "ops": 2**31 - 1}) == {"team": 3, "ops": 2**31 - 1}


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        {"team": True},
        {"team": 0},
        {"team": 2**31},
        {"team": "3"},
        {"team": 3.0},
        {"": 3},
        {"   ": 3},
        {"x" * 256: 3},
        {"team": 3, " team": 4},  # the same key after trimming
        {f"g{i}": 1 for i in range(sync.MAX_MAPPING_PAIRS + 1)},
    ],
)
def test_the_normalizer_refuses(value):
    with pytest.raises(ValueError):
        sync.normalize_mapping(value)


@pytest.mark.parametrize("value", [None, [], {"team": True}, {"team": 2**31}, {" team": 3}])
def test_a_damaged_stored_mapping_reads_as_none(value):
    assert sync.stored_mapping(value) is None


def test_a_valid_stored_mapping_reads_as_itself():
    assert sync.stored_mapping({}) == {}
    assert sync.stored_mapping({"team": 1}) == {"team": 1}


# ── exp (S35, S39) ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("exp,expected", [(1_900_000_000, 1_900_000_000), (1_900_000_000.99, 1_900_000_000)])
def test_exp_is_floored(exp, expected):
    assert sync.exp_seconds({"exp": exp}) == expected


@pytest.mark.parametrize("exp", [None, True, False, "1900000000", math.inf, -math.inf, math.nan, 10**30])
def test_unusable_exp(exp):
    claims = {} if exp is None else {"exp": exp}
    with pytest.raises(ValueError):
        sync.exp_seconds(claims)


def test_the_deadline_is_the_earlier_of_the_window_and_exp(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(sync, "now_utc", lambda: datetime.fromtimestamp(1_000_000, tz=timezone.utc))
    assert sync.login_deadline(2_000_000) == 1_000_000 + sync.EVIDENCE_WINDOW_SECONDS
    assert sync.login_deadline(1_000_100) == 1_000_100


# ── evidence (spec §C.2) ─────────────────────────────────────────────────────


def _evidence(**changes):
    value = sync.build_evidence(provider_id=4, revision="r" * 32, sub="sub", groups=["a"], deadline=1_000)
    value.update(changes)
    return value


def test_evidence_round_trip():
    evidence = sync.read_evidence(_evidence())
    assert evidence == sync.Evidence(4, "r" * 32, "sub", ("a",), 1_000)
    assert sync.read_evidence(_evidence(groups=None)).groups is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"v": 2},
        _evidence(v=True),
        _evidence(provider_id=True),
        _evidence(provider_id=0),
        _evidence(revision=""),
        _evidence(revision="r" * 33),
        _evidence(sub=""),
        _evidence(groups="a"),
        _evidence(groups=[1]),
        _evidence(deadline="1000"),
        _evidence(deadline=1000.5),
        {k: v for k, v in _evidence().items() if k != "groups"},  # a missing key is no empty mapping (N07-A02)
        {k: v for k, v in _evidence().items() if k != "deadline"},
    ],
)
def test_malformed_evidence_reads_as_none(raw):
    assert sync.read_evidence(raw) is None


def test_the_deadline_is_exclusive(monkeypatch):
    from datetime import datetime, timezone

    evidence = sync.read_evidence(_evidence(deadline=1_000))
    monkeypatch.setattr(sync, "now_utc", lambda: datetime.fromtimestamp(999.999, tz=timezone.utc))
    assert sync.evidence_is_live(evidence)
    monkeypatch.setattr(sync, "now_utc", lambda: datetime.fromtimestamp(1_000, tz=timezone.utc))
    assert not sync.evidence_is_live(evidence)


def test_state_evidence():
    assert sync.state_revision(sync.state_evidence("abc")) == "abc"
    assert sync.state_revision(None) is None
    assert sync.state_revision({"v": True, "revision": "abc"}) is None
