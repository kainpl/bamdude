"""Routing intent v2: the identity is the snapshot's HASH, and v1 still means what it meant.

Spec: ``60-specs/queue-source-spool-spec.md`` §7 and A03. Until m173 a routing
intent described the job by its ORIGINAL file — ``{kind, id}`` plus that file's
``(size, mtime_ns)``. A job that prints a frozen copy has neither: the original
row may be gone, and the copy's mtime is the moment BamDude wrote the bytes, so
it changes on a portable restore, on a file-level restore and on any tooling
that rewrites the spool while every byte stays the same.

Two halves have to move together (the Task 6 review's ruling): the writer stops
stamping an mtime and starts stamping the hash, and the reader stops ignoring
the revision for snapshot-backed rows. A LEGACY row must still be deferred as
``source_changed`` when its original changes, and a row whose intent was written
before this version — a v1 stamp of a snapshot's mtime — must still dispatch.
"""

import json
from types import SimpleNamespace

import pytest

from backend.app.services.filament_policy import (
    SUPPORTED_VERSIONS,
    VERSION,
    deserialize_policy,
    restore_routing_source,
    serialize_policy,
)
from backend.app.services.filament_preflight import revision_for
from backend.app.services.filament_requirements import (
    HASH_REVISION_KEYS,
    STAT_REVISION_KEYS,
    PrintRequirements,
    SourceIdentity,
    revision_refutes,
)
from backend.app.services.filament_routing import RoutingPolicy
from backend.app.services.printer_feed_snapshot import PrinterFeedSnapshot

SHA = "a" * 64
OTHER_SHA = "b" * 64


def a_captured_identity(*, sha256: str = SHA, size: int = 4096, mtime_ns: int = 111) -> SourceIdentity:
    """What a read of a published snapshot produces."""
    return SourceIdentity("/data/queue-sources/aa/bb/object.3mf", size, mtime_ns, sha256)


def a_legacy_identity(*, size: int = 4096, mtime_ns: int = 111) -> SourceIdentity:
    """What a read of an original file produces — no hash was computed for it."""
    return SourceIdentity("/share/models/lamp.gcode.3mf", size, mtime_ns)


def requirements_of(identity: SourceIdentity | None, *, plate: int = 15) -> PrintRequirements:
    return PrintRequirements(status="ok", source_identity=identity, resolved_plate_id=plate)


def a_pinned_policy() -> RoutingPolicy:
    return RoutingPolicy(
        mode="pinned",
        feed_policy="external_only",
        force_color_match=True,
        filament_overrides=({"slot_id": 1, "type": "PLA", "color": "FF0000"},),
        physical_pins={1: {"source_id": 254, "type": "PLA", "color": "FF0000", "nozzles": [0]}},
        review_required=True,
    )


# --------------------------------------------------------------------------- #
# The identity itself
# --------------------------------------------------------------------------- #


def test_a_captured_identity_is_its_hash_and_never_its_mtime():
    """A restore rewrites every spool mtime and not one byte — see the module docstring."""
    restored = a_captured_identity(mtime_ns=999)
    assert restored == a_captured_identity()
    assert hash(restored) == hash(a_captured_identity())
    # The hash is what the comparison is FOR: different bytes are a different source.
    assert a_captured_identity(sha256=OTHER_SHA) != a_captured_identity()
    # A truncated copy of the same declared hash is still not the same evidence.
    assert a_captured_identity(size=5) != a_captured_identity()


def test_an_original_is_still_identified_by_its_size_and_mtime():
    assert a_legacy_identity(mtime_ns=999) != a_legacy_identity()
    assert a_legacy_identity(size=5) != a_legacy_identity()
    assert a_legacy_identity() == a_legacy_identity()
    # A labelled and an unlabelled read of the same file answer different
    # questions and must never compare equal by accident.
    assert a_captured_identity() != SourceIdentity(a_captured_identity().path, 4096, 111)


def test_a_read_still_notices_a_file_mutating_under_it():
    """``same_stat_as`` is the check INSIDE one read, where the mtime is evidence."""
    identity = a_captured_identity()
    assert identity.same_stat_as(a_captured_identity())
    assert not identity.same_stat_as(a_captured_identity(mtime_ns=112))


def test_the_two_stored_revision_shapes_are_distinct():
    assert a_captured_identity().revision() == {"sha256": SHA, "size_bytes": 4096}
    assert a_legacy_identity().revision() == {"size": 4096, "mtime_ns": 111}
    assert HASH_REVISION_KEYS != STAT_REVISION_KEYS


# --------------------------------------------------------------------------- #
# What a stored revision may refute
# --------------------------------------------------------------------------- #


def test_a_restored_spool_does_not_refute_a_captured_intent():
    stored = a_captured_identity().revision()
    assert not revision_refutes(stored, a_captured_identity(mtime_ns=42))


def test_different_bytes_under_the_same_blob_do_refute_it():
    stored = a_captured_identity().revision()
    assert revision_refutes(stored, a_captured_identity(sha256=OTHER_SHA))
    assert revision_refutes(stored, a_captured_identity(size=1))


def test_a_legacy_row_is_still_refuted_when_its_original_changes():
    """The other half of the Task 6 ruling: nothing about a legacy row got easier."""
    stored = a_legacy_identity().revision()
    assert revision_refutes(stored, a_legacy_identity(mtime_ns=222))
    assert revision_refutes(stored, a_legacy_identity(size=1))
    assert not revision_refutes(stored, a_legacy_identity())


def test_a_version_one_stamp_of_a_snapshot_is_not_read_as_an_identity():
    """The trap the Task 6 reader-ignore closed, and this must not reopen it.

    An intent written before v2 recorded the COPY's ``(size, mtime_ns)``. Reading
    it against a captured source would defer every assigned-but-undispatched job
    after a restore, so a revision that cannot speak about the hash says nothing.
    """
    assert not revision_refutes({"size": 4096, "mtime_ns": 111}, a_captured_identity(mtime_ns=999))
    assert not revision_refutes({"size": 1, "mtime_ns": 1}, a_captured_identity())


@pytest.mark.parametrize("stored", [{"revision": 1}, {"sha256": SHA}, {"size": 1}, {"size_bytes": 1, "mtime_ns": 1}, 7])
def test_an_unrecognised_revision_fails_closed(stored):
    """Evidence this version cannot read is a refusal, never a shrug."""
    assert revision_refutes(stored, a_captured_identity())
    assert revision_refutes(stored, a_legacy_identity())


def test_no_stored_revision_and_no_current_one_refute_nothing():
    assert not revision_refutes(None, a_captured_identity())
    assert not revision_refutes({}, a_captured_identity())
    assert not revision_refutes(a_captured_identity().revision(), None)


def test_the_blocked_revision_fingerprint_follows_the_hash_not_the_mtime():
    """``runtime.blocked_revision`` is persisted, so it must survive a restore too."""
    snapshot = PrinterFeedSnapshot(1, "P1P", True, 1, "marker", True, True, True, ())
    policy = RoutingPolicy()
    restored = revision_for(requirements_of(a_captured_identity(mtime_ns=999)), policy, snapshot)
    assert restored == revision_for(requirements_of(a_captured_identity()), policy, snapshot)
    assert restored != revision_for(requirements_of(a_captured_identity(sha256=OTHER_SHA)), policy, snapshot)
    assert restored != revision_for(requirements_of(a_legacy_identity()), policy, snapshot)


# --------------------------------------------------------------------------- #
# The payload the writer produces
# --------------------------------------------------------------------------- #


def test_the_writer_stamps_version_two_with_the_blob_and_its_hash():
    stored = json.loads(
        serialize_policy(
            a_pinned_policy(),
            library_file_id=7,
            requirements=requirements_of(a_captured_identity()),
            printer_id=3,
            exact_model=True,
            queue_source_id=4,
        )
    )
    assert stored["version"] == 2 == VERSION
    assert stored["source_identity"] == {
        "kind": "library",
        "id": 7,
        "queue_source_id": 4,
        "revision": {"sha256": SHA, "size_bytes": 4096},
    }
    # The resolved plate still comes off the requirements, and the printer scope
    # and the exact-model rule still travel.
    assert (stored["resolved_plate_id"], stored["printer_id"], stored["exact_model"]) == (15, 3, True)


def test_a_legacy_read_still_records_the_originals_size_and_mtime():
    stored = json.loads(
        serialize_policy(RoutingPolicy(), archive_id=9, requirements=requirements_of(a_legacy_identity()))
    )
    assert stored["source_identity"] == {"kind": "archive", "id": 9, "revision": {"size": 4096, "mtime_ns": 111}}
    assert "queue_source_id" not in stored["source_identity"]


def test_a_source_that_could_not_be_identified_records_no_revision():
    stored = json.loads(serialize_policy(RoutingPolicy(), library_file_id=7, requirements=requirements_of(None)))
    assert stored["source_identity"] == {"kind": "library", "id": 7}


# --------------------------------------------------------------------------- #
# The decoder reads 1 and 2, and nothing else
# --------------------------------------------------------------------------- #


def test_a_version_one_payload_still_decodes_to_exactly_what_it_meant():
    policy = a_pinned_policy()
    v1 = json.loads(serialize_policy(policy, library_file_id=7, plate_id=15, printer_id=3, exact_model=True))
    v1["version"] = 1
    v1["source_identity"] = {"kind": "library", "id": 7, "revision": {"size": 10, "mtime_ns": 20}}
    assert deserialize_policy(json.dumps(v1)) == policy


def test_a_version_two_payload_keeps_its_pins_scope_and_review_rules():
    policy = a_pinned_policy()
    stored = serialize_policy(
        policy,
        library_file_id=7,
        requirements=requirements_of(a_captured_identity()),
        printer_id=3,
        exact_model=True,
        queue_source_id=4,
    )
    assert deserialize_policy(stored) == policy
    raw = json.loads(stored)
    assert raw["physical_pins"] == {"1": policy.physical_pins[1]}
    assert raw["review_required"] is True
    assert raw["force_color_match"] is True
    assert list(raw["filament_overrides"]) == list(policy.filament_overrides)


def test_upgrading_a_version_one_intent_carries_every_semantic_choice():
    """Re-writing a v1 intent as v2 is only allowed to change the identity.

    The one thing an edit is entitled to replace is the evidence about the FILE;
    the pins, the feed policy, the overrides, the printer scope, the resolved
    plate, ``exact_model`` and the review flag are the operator's answers and
    must come out the other side untouched.
    """
    policy = a_pinned_policy()
    v1 = json.loads(serialize_policy(policy, library_file_id=7, plate_id=15, printer_id=3, exact_model=True))
    v1["version"] = 1
    v2 = json.loads(
        serialize_policy(
            deserialize_policy(json.dumps(v1)),
            library_file_id=v1["source_identity"]["id"],
            requirements=requirements_of(a_captured_identity()),
            printer_id=v1["printer_id"],
            exact_model=v1["exact_model"],
            queue_source_id=4,
        )
    )
    assert v2["version"] == 2
    assert {k: v for k, v in v2.items() if k not in ("version", "source_identity")} == {
        k: v for k, v in v1.items() if k not in ("version", "source_identity")
    }
    assert deserialize_policy(json.dumps(v2)) == deserialize_policy(json.dumps(v1)) == policy


@pytest.mark.parametrize("version", [0, 3, 99, -1, "2", 2.0, None, True])
def test_an_unknown_version_is_still_a_refusal(version):
    """Accepting two versions must not become accepting whatever arrives.

    A forward-compatibility path that proceeds with a payload it does not
    understand is worse than one that refuses: the pins and the review flag ARE
    the meaning here, so an unreadable intent is ``review_required`` and a human
    looks at it.
    """
    # A policy that is otherwise perfectly readable and NOT already
    # ``review_required``, so the version is the only thing that can refuse it.
    payload = json.loads(serialize_policy(RoutingPolicy(mode="pinned"), library_file_id=7))
    assert not deserialize_policy(json.dumps(payload)).review_required
    payload["version"] = version
    assert deserialize_policy(json.dumps(payload)).review_required


def test_the_supported_versions_are_exactly_one_and_two():
    assert SUPPORTED_VERSIONS == (1, 2)
    assert VERSION in SUPPORTED_VERSIONS


# --------------------------------------------------------------------------- #
# Repeat / Retry / clone still restore the source a v1 intent describes
# --------------------------------------------------------------------------- #


class _Row:
    def __init__(self, routing, **columns):
        self.filament_routing = routing
        self.archive_id = columns.get("archive_id")
        self.library_file_id = columns.get("library_file_id")
        self.queue_source_id = columns.get("queue_source_id")


@pytest.mark.parametrize("version", [1, 2])
def test_repeat_restores_the_source_of_a_payload_this_version_understands(version):
    payload = json.loads(
        serialize_policy(
            RoutingPolicy(),
            library_file_id=7,
            requirements=requirements_of(a_captured_identity()),
            queue_source_id=4,
        )
    )
    payload["version"] = version
    payload["runtime"] = {"reason": "feed_state_changed", "blocked_revision": "x"}
    row = _Row(json.dumps(payload), archive_id=88, queue_source_id=4)
    restore_routing_source(row)
    assert (row.library_file_id, row.archive_id) == (7, None)
    # The blob reference is the ROW's and is never re-attached from a payload:
    # re-attaching one needs the storage guard and a live ``ready`` check.
    assert row.queue_source_id == 4
    assert "runtime" not in json.loads(row.filament_routing)


def test_repeat_leaves_a_payload_it_cannot_read_alone():
    payload = json.loads(serialize_policy(RoutingPolicy(), library_file_id=7))
    payload["version"] = 3
    row = _Row(json.dumps(payload), archive_id=88)
    restore_routing_source(row)
    assert (row.library_file_id, row.archive_id) == (None, 88)


def test_a_captured_read_still_refuses_a_file_that_moved_under_the_parse(tmp_path, monkeypatch):
    """The single-read mutation guard did NOT get weaker when identity moved to the hash.

    ``read_print_requirements`` stats the file before and after the ZIP work. Only
    the mtime changes here — the size is identical — so a guard that compared the
    hash-anchored identities would see two equal values and return the parse of a
    file that was rewritten while it was being read.
    """
    from backend.app.services import filament_requirements as fr
    from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

    source = write_routing_3mf(tmp_path / "changing.3mf", {1: [{"id": 1, "type": "PLA", "used_g": "1"}]})
    real = SourceIdentity.of
    calls = []

    def moving(path, *, sha256=None):
        identity = real(path, sha256=sha256)
        calls.append(identity)
        return (
            identity if len(calls) == 1 else SourceIdentity(identity.path, identity.size, identity.mtime_ns + 1, sha256)
        )

    monkeypatch.setattr(SourceIdentity, "of", moving)
    result = fr.read_print_requirements(source, sha256=SHA)
    assert result.reason == "source_changed"


def test_an_intent_about_a_captured_blob_refuses_a_file_that_is_not_that_blob():
    """The OTHER shape mismatch, and it is never legitimate (review m1).

    Stored hash-shaped against a stat-anchored read means the intent was written
    about a captured object and is being checked against a file that is not that
    object. ``item_descriptor`` documents exactly how a row gets there — a
    ``queue_source_id`` whose row is gone "reads as legacy, its own original" —
    and such a row would otherwise dispatch a possibly re-sliced original with no
    changed-file check at all.
    """
    stored = a_captured_identity().revision()
    assert revision_refutes(stored, a_legacy_identity())
    # And the legitimate direction stays silent: a pre-v2 stamp of a snapshot.
    assert not revision_refutes(a_legacy_identity().revision(), a_captured_identity())


def test_recording_the_blob_does_not_mutate_the_callers_payload():
    """``decode`` hands a dict straight back, and the writers call this in a loop."""
    from backend.app.services.filament_policy import record_queue_source

    payload = json.loads(serialize_policy(RoutingPolicy(), library_file_id=7))
    before = json.loads(json.dumps(payload))
    stamped = json.loads(record_queue_source(payload, SimpleNamespace(id=4)))
    assert payload == before
    assert stamped["source_identity"]["queue_source_id"] == 4
