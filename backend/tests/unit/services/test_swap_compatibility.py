"""A source's declaration of its baked swap sequence: one classifier, one strict reader, one writer, one policy.

Swap-source-lifecycle spec r4 §2-§4, §8; plan T1. True, False and unknown (None) are three states that survive
the whole path. A display name is classified only once, when new external bytes arrive; afterwards the fact belongs
to the source and is read strictly. An archive without a recorded fact is unknown unless its column is strictly True
(D1): dispatch never wrote the column, so a legacy False says nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services import swap_compatibility as sc

START, TABLE = "swap_mode_start", "swap_mode_change_table"


@pytest.mark.parametrize("snapshot", [{"version": 1, "provenance": "a"}, {"version": 20}, ["legacy"], None])
def test_normalizing_a_snapshot_only_removes_the_current_swap_fact(snapshot):
    for fact in (sc.SourceFact(True), sc.SourceFact(False), *(sc.SourceFact(None, r) for r in sc.UNKNOWN_REASONS)):
        copy = (
            sc.snapshot_with_fact(snapshot, fact)
            if isinstance(snapshot, dict) and snapshot.get("version") == 1
            else snapshot
        )
        assert sc.snapshot_without_fact(copy) == sc.snapshot_without_fact(snapshot)
        assert sc.same_snapshot_for_return(snapshot, copy)


def _archive(column=False, extra=None):
    return SimpleNamespace(swap_compatible=column, extra_data=extra)


def _carrier(value, pending=False, version=1):
    return {"version": version, "value": value, "pending_external": pending}


# --- archive reader (§3, D1) ----------------------------------------------------------------------------------------


def test_archive_legacy_false_is_unknown_but_true_survives():
    assert sc.archive_fact(_archive(False, None)).value is None
    assert sc.archive_fact(_archive(False, {"plate_id": 1})).value is None
    assert sc.archive_fact(_archive(True, None)).value is True
    assert sc.archive_fact(_archive(True, {"plate_id": 1})).value is True
    # a column that is not strictly bool is not a legacy True
    assert sc.archive_fact(_archive(1, None)).value is None


def test_a_valid_carrier_is_read_with_its_three_values():
    assert sc.archive_fact(_archive(True, {sc.CARRIER_KEY: _carrier(True)})).value is True
    assert sc.archive_fact(_archive(False, {sc.CARRIER_KEY: _carrier(False)})).value is False
    unknown = sc.archive_fact(_archive(False, {sc.CARRIER_KEY: _carrier(None)}))
    assert unknown.value is None and unknown.pending_external is False and unknown.recorded is True
    pending = sc.archive_fact(_archive(False, {sc.CARRIER_KEY: _carrier(None, pending=True)}))
    assert pending.value is None and pending.pending_external is True


@pytest.mark.parametrize(
    "carrier",
    [
        _carrier(True, version=True),
        _carrier(True, version=1.0),
        _carrier(True, version="1"),
        _carrier(True, version=2),
        _carrier(True, version=None),
        _carrier(0),
        _carrier(1),
        _carrier("false"),
        _carrier([]),
        {"version": 1, "value": True},  # no pending_external
        {"version": 1, "pending_external": False},  # no value
        _carrier(None, pending="no"),
        _carrier(None, pending=0),
        _carrier(None, pending=None),
        _carrier(True, pending=True),
        _carrier(False, pending=True),
        ["version", 1],
        "swap",
    ],
    ids=lambda c: repr(c)[:40],
)
def test_carrier_types_are_strict(carrier):
    # a malformed record is unknown and opens nothing: no legacy fallback to the column, no attach right
    for column in (True, False):
        fact = sc.archive_fact(_archive(column, {sc.CARRIER_KEY: carrier}))
        assert fact.value is None
        assert fact.pending_external is False


@pytest.mark.parametrize("extra", [["plate_id", 1], "metadata", 42])
def test_extra_data_that_is_not_a_mapping_is_unknown_not_legacy(extra):
    assert sc.archive_fact(_archive(True, extra)).value is None


@pytest.mark.parametrize(
    ("value", "column"),
    [(True, False), (False, True), (None, True)],
)
def test_conflicting_projection_is_unknown_without_fallback(value, column):
    fact = sc.archive_fact(_archive(column, {sc.CARRIER_KEY: _carrier(value)}))
    assert fact.value is None and fact.pending_external is False


# --- archive writer (§3) --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False, None])
def test_archive_writer_keeps_other_metadata_and_controls_reserved_key(value):
    original = {"plate_id": 2, "no_3mf_available": True, sc.CARRIER_KEY: "forged"}
    archive = _archive(not value if isinstance(value, bool) else True, original)

    sc.write_archive_fact(archive, value)

    assert archive.swap_compatible is (value is True)
    assert archive.extra_data == {
        "plate_id": 2,
        "no_3mf_available": True,
        sc.CARRIER_KEY: {"version": 1, "value": value, "pending_external": False},
    }
    assert original[sc.CARRIER_KEY] == "forged"  # a new dict: the ORM sees the change, the old one is untouched
    assert sc.archive_fact(archive).value is value


def test_archive_writer_records_a_pending_external_classification():
    archive = _archive(True, None)
    sc.write_archive_fact(archive, None, pending_external=True)
    fact = sc.archive_fact(archive)
    assert archive.swap_compatible is False
    assert fact.value is None and fact.pending_external is True


@pytest.mark.parametrize(
    ("value", "pending"),
    [(True, True), (False, True), (1, False), ("true", False)],
)
def test_archive_writer_refuses_what_the_reader_would_not_accept(value, pending):
    with pytest.raises(ValueError):
        sc.write_archive_fact(_archive(False, None), value, pending_external=pending)


def test_parsed_source_metadata_cannot_carry_the_reserved_key():
    parsed = {"print_time_seconds": 60, sc.CARRIER_KEY: _carrier(True)}
    clean = sc.strip_reserved(parsed)
    assert clean == {"print_time_seconds": 60}
    assert sc.CARRIER_KEY in parsed  # the caller's dict is not mutated


def test_the_api_copy_hides_only_the_carrier():
    extra = {"plate_id": 1, sc.CARRIER_KEY: _carrier(False)}
    public = sc.public_extra_data(extra)
    assert public == {"plate_id": 1}
    assert sc.CARRIER_KEY in extra
    assert sc.public_extra_data(None) is None


# --- library reader (§4) --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "file_hash", "expected"),
    [
        (True, "a" * 64, True),
        (False, "0123456789abcdef" * 4, False),
        (True, None, None),
        (False, None, None),
        (False, "", None),
        (True, "abc", None),
        (True, "A" * 64, None),
        (True, "g" * 64, None),
        (1, "a" * 64, None),
    ],
)
def test_library_fact_requires_content_identity(column, file_hash, expected):
    # A managed row: an external one's legacy False is classified by its name on the share (T9 §8, C).
    row = SimpleNamespace(swap_compatible=column, file_hash=file_hash, is_external=False, file_path="library/x.3mf")
    assert sc.library_fact(row) is expected


# --- classifier (§4) ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("lamp.swap.gcode.3mf", True),
        ("lamp.swaps.gcode.3mf", True),
        ("Lamp.SWAP.3mf", True),
        ("/sdcard/lamp.swap.gcode.3mf", True),
        ("C:\\farm\\lamp.swaps.gcode.3mf", True),
        ("my.swap.dir/lamp.gcode.3mf", False),
        ("C:\\a.swaps.b\\lamp.gcode.3mf", False),
        ("swaps.3mf", False),
        ("lamp swaps.gcode.3mf", False),
        ("lamp.swap", False),
        ("lampswap.3mf", False),
        ("lamp.swapper.3mf", False),
        ("", False),
        (None, False),
    ],
)
def test_classifier_uses_only_real_basename(name, expected):
    assert sc.classify_external_name(name) is expected


# --- policy (§2, §8) ------------------------------------------------------------------------------------------------


def test_effective_request_requires_events():
    assert sc.effective_request(execute=True, events=None, printer_swap_mode=True) is False
    assert sc.effective_request(execute=True, events=[], printer_swap_mode=True) is False
    assert sc.effective_request(execute=True, events=[START], printer_swap_mode=True) is True
    assert sc.effective_request(execute=True, events=[START, TABLE], printer_swap_mode=False) is False
    assert sc.effective_request(execute=True, events=["bogus"], printer_swap_mode=True) is False
    assert sc.effective_request(execute=True, events=[START, "bogus"], printer_swap_mode=True) is False
    assert sc.effective_request(execute=True, events=START, printer_swap_mode=True) is False  # not a list
    # an operator's opt-out is never raised into a request
    assert sc.effective_request(execute=False, events=[START, TABLE], printer_swap_mode=True) is False
    assert sc.effective_request(execute=None, events=[START], printer_swap_mode=True) is False


def test_policy_refuses_unknown_requested_macros():
    refused = sc.decide(None, execute=True, events=[START, TABLE], printer_swap_mode=True)
    assert refused.refused is True and refused.reason == "swap_source_unknown"
    assert refused.execute is False and refused.events is None

    plain = sc.decide(None, execute=True, events=None, printer_swap_mode=True)
    assert plain.refused is False and plain.execute is False and plain.events is None


def test_policy_runs_only_what_a_known_false_source_requested():
    allowed = sc.decide(False, execute=True, events=[TABLE], printer_swap_mode=True)
    assert allowed.refused is False and allowed.execute is True and allowed.events == (TABLE,)

    opted_out = sc.decide(False, execute=False, events=[START, TABLE], printer_swap_mode=True)
    assert opted_out.execute is False and opted_out.events is None

    no_swap_mode = sc.decide(False, execute=True, events=[START], printer_swap_mode=False)
    assert no_swap_mode.execute is False and no_swap_mode.refused is False


def test_policy_mutes_ours_when_the_source_carries_its_own():
    baked = sc.decide(True, execute=True, events=[START, TABLE], printer_swap_mode=True)
    assert baked.refused is False and baked.execute is False and baked.events is None


@pytest.mark.parametrize("fact", [0, 1, "false", "true"])
def test_policy_treats_a_non_bool_fact_as_unknown(fact):
    assert sc.decide(fact, execute=True, events=[START], printer_swap_mode=True).refused is True


def test_projection_preserves_wire_bool():
    assert sc.project(True) == (True, True)
    assert sc.project(False) == (False, True)
    assert sc.project(None) == (False, False)


# --- a job's own fact (§5, §6) --------------------------------------------------------------------------------------

H1, H2 = "a" * 64, "b" * 64


def test_a_rejected_record_says_so():
    assert sc.archive_fact(_archive(True, {sc.CARRIER_KEY: "forged"})).invalid is True
    assert sc.archive_fact(_archive(False, None)).invalid is False  # legacy is not a rejection
    archive = _archive(
        False,
        {sc.CARRIER_KEY: "forged"},
    )
    archive.source_content_hash = H1
    assert sc.archive_source(archive) == (sc.SourceFact(None, "record_invalid"), H1)


def test_archive_source_expects_the_unpatched_hash_first():
    archive = _archive(False, {sc.CARRIER_KEY: _carrier(False)})
    archive.source_content_hash, archive.content_hash = H1, H2
    assert sc.archive_source(archive) == (sc.SourceFact(False), H1)
    archive.source_content_hash = None
    assert sc.archive_source(archive) == (sc.SourceFact(False), H2)
    legacy = _archive(False, None)
    legacy.source_content_hash = H1
    assert sc.archive_source(legacy) == (sc.SourceFact(None, "source_unknown"), H1)


def test_library_source_needs_a_valid_hash():
    assert sc.library_source(SimpleNamespace(swap_compatible=True, file_hash=H1)) == (sc.SourceFact(True), H1)
    assert sc.library_source(SimpleNamespace(swap_compatible=False, file_hash=None)) == (
        sc.SourceFact(None, "hash_missing"),
        None,
    )


@pytest.mark.parametrize(
    ("fact", "expected", "actual", "result"),
    [
        (sc.SourceFact(True), H1, H1, sc.SourceFact(True)),
        (sc.SourceFact(False), H1, H1, sc.SourceFact(False)),
        (sc.SourceFact(False), H1, H2, sc.SourceFact(None, "hash_mismatch")),
        (sc.SourceFact(False), None, H1, sc.SourceFact(None, "hash_missing")),
        (sc.SourceFact(True), "A" * 64, "A" * 64, sc.SourceFact(None, "hash_missing")),
        (sc.SourceFact(None, "record_invalid"), H1, H1, sc.SourceFact(None, "record_invalid")),
        (sc.SourceFact(None, missing=True), H1, H1, sc.SourceFact(None, "source_unknown")),
    ],
)
def test_the_receipt_confirms_the_declaration_against_the_real_hash(fact, expected, actual, result):
    assert sc.confirm(fact, expected, actual) == result


@pytest.mark.parametrize(
    ("payload", "fact"),
    [
        ({}, sc.SourceFact(None, missing=True)),
        ({sc.SNAPSHOT_FACT_KEY: True}, sc.SourceFact(True)),
        ({sc.SNAPSHOT_FACT_KEY: False}, sc.SourceFact(False)),
        ({sc.SNAPSHOT_FACT_KEY: None}, sc.SourceFact(None, "source_unknown")),
        ({sc.SNAPSHOT_FACT_KEY: None, sc.SNAPSHOT_REASON_KEY: "hash_missing"}, sc.SourceFact(None, "hash_missing")),
        ({sc.SNAPSHOT_FACT_KEY: None, sc.SNAPSHOT_REASON_KEY: "granted"}, sc.SourceFact(None, "source_unknown")),
        ({sc.SNAPSHOT_FACT_KEY: 0}, sc.SourceFact(None, "record_invalid")),
        ({sc.SNAPSHOT_FACT_KEY: "false"}, sc.SourceFact(None, "record_invalid")),
        # a reason never turns a value into something else
        ({sc.SNAPSHOT_FACT_KEY: False, sc.SNAPSHOT_REASON_KEY: "hash_mismatch"}, sc.SourceFact(False)),
    ],
)
def test_the_snapshot_fact_is_read_strictly(payload, fact):
    assert sc.snapshot_fact(payload) == fact


def test_a_copy_records_an_answer_and_never_shares_the_dict():
    parent = {"version": 1, "provenance": {"kind": "library_file", "id": 3}, sc.SNAPSHOT_REASON_KEY: "hash_missing"}
    copy = sc.snapshot_with_fact(parent, sc.SourceFact(True))
    assert copy == {"version": 1, "provenance": {"kind": "library_file", "id": 3}, sc.SNAPSHOT_FACT_KEY: True}
    assert copy["provenance"] is not parent["provenance"]
    assert sc.SNAPSHOT_FACT_KEY not in parent
    unanswered = sc.snapshot_with_fact(parent, sc.SourceFact(None, missing=True))
    assert unanswered[sc.SNAPSHOT_FACT_KEY] is None and unanswered[sc.SNAPSHOT_REASON_KEY] == "source_unknown"


def test_legacy_resolution_takes_exactly_one_own_link():
    library = SimpleNamespace(swap_compatible=True, file_hash=H1)
    archive = _archive(False, {sc.CARRIER_KEY: _carrier(False)})
    archive.source_content_hash = H1

    def resolve(**kwargs):
        return sc.resolve_legacy(context="source", **kwargs)

    assert resolve(provenance_kind="library_file", archive=archive, library_file=library, sha256=H1) == sc.SourceFact(
        True
    )
    assert resolve(provenance_kind="archive", archive=archive, library_file=library, sha256=H1) == sc.SourceFact(False)
    assert resolve(provenance_kind=None, archive=archive, library_file=library, sha256=H1) == sc.UNKNOWN_SOURCE
    assert resolve(provenance_kind=None, archive=None, library_file=None, sha256=H1) == sc.UNKNOWN_SOURCE
    assert resolve(provenance_kind="share", archive=None, library_file=library, sha256=H1) == sc.UNKNOWN_SOURCE
    assert resolve(provenance_kind=None, archive=None, library_file=library, sha256=H2) == sc.SourceFact(
        None, "hash_mismatch"
    )
