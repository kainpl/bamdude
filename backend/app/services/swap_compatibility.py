"""A source's declaration of its own baked swap sequence (swap-source-lifecycle spec r4 §2-§4, §8).

``swap_compatible`` is what the SOURCE declares: that its bytes already carry a plate-swap sequence. It is not an
analysis of the G-code and not a bed sensor. The fact has three states that must survive the whole path -- capture,
queue, execution archive, reprint, backup: True, False and unknown (``None``). False is as authoritative as True;
unknown is not False.

One place answers each question:

- :func:`classify_external_name` -- the only filename rule, applied once, when NEW external bytes arrive (upload,
  ZIP member, scan, virtual printer, an observed external start). Never to an already known source: a display
  rename changes nothing.
- :func:`archive_fact` / :func:`write_archive_fact` -- the archive's record, ``extra_data["swap_source"]``, with the
  bool column kept as its projection (``column == (value is True)``). The column is for display and for legacy
  readers only; no macro or plate-clear gate may read it directly.
- :func:`library_fact` -- a library row's declaration counts only with a valid content hash.
- :class:`SourceFact` -- a JOB's own declaration: taken at capture (:func:`archive_source` / :func:`library_source`),
  confirmed against the hash the worker computed (:func:`confirm`), kept in the job's ``source_snapshot``
  (:func:`snapshot_fact`, :func:`snapshot_with_fact`), and for a snapshot that predates the fix answered once from
  the job's own links (:func:`resolve_legacy`).
- :func:`decide` -- whether OUR swap macros may run for a job, and what an unknown source does to a request for them.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

#: The reserved ``PrintArchive.extra_data`` key holding the record. Parsed 3MF metadata never writes it.
CARRIER_KEY = "swap_source"
CARRIER_VERSION = 1
#: The events BamDude's own swap macros fire on (``routes/macros.py``).
SWAP_EVENTS = frozenset({"swap_mode_start", "swap_mode_change_table"})
#: The refusal a request for our macros gets on a source whose declaration is unknown (D2).
UNKNOWN_SOURCE_REASON = "swap_source_unknown"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MARKER = re.compile(r"\.swaps?\.", re.IGNORECASE)


@dataclass(frozen=True)
class ArchiveFact:
    """What an archive declares. ``recorded`` is False for a legacy row with no record (or an unreadable one);
    ``pending_external`` is True only for a valid record of an observed external start whose file has not been
    classified yet -- the one state a late attach may finish. ``invalid``: a record is there and was rejected."""

    value: bool | None
    pending_external: bool = False
    recorded: bool = False
    invalid: bool = False


@dataclass(frozen=True)
class SwapDecision:
    """``execute`` / ``events``: what this job may fire of OUR swap macros. ``refused``: an effective request met an
    unknown source (D2) -- the job must not start silently without the swap the operator asked for."""

    execute: bool
    events: tuple[str, ...] | None
    refused: bool = False
    reason: str | None = None


# --- classifier -----------------------------------------------------------------------------------------------------


def classify_external_name(name: str | None) -> bool:
    """True when the basename of a NEW external file declares a baked swap sequence.

    The marker is dot-delimited on both sides -- ``.swap.`` or ``.swaps.``, in any case -- and only in the basename,
    split on both path separators: a marker in a directory, the bare word "swaps" or a stem that ends in ``.swap``
    with no further suffix declares nothing. This narrows the old substring match over the whole string.
    """
    if not isinstance(name, str):
        return False
    basename = re.split(r"[\\/]", name)[-1]
    return _MARKER.search(basename) is not None


# --- archive record -------------------------------------------------------------------------------------------------


def _strict_fact(value: Any) -> bool | None:
    return value if type(value) is bool else None


def archive_fact(archive) -> ArchiveFact:
    """The archive's declaration, read strictly.

    A valid record wins, provided the bool column agrees with it. A record that is malformed, of another version,
    or contradicted by the column is unknown -- with no fallback to the column and no attach right. With no record
    at all (a row from before this fix) only a strictly True column survives: dispatch never wrote the column, so
    a legacy False declares nothing (D1). ``extra_data`` that is not a mapping is unknown too.
    """
    column = getattr(archive, "swap_compatible", None)
    extra = getattr(archive, "extra_data", None)
    if extra is None or (isinstance(extra, Mapping) and CARRIER_KEY not in extra):
        return ArchiveFact(True if column is True else None)
    rejected = ArchiveFact(None, invalid=True)
    if not isinstance(extra, Mapping):
        return rejected
    record = extra[CARRIER_KEY]
    if not isinstance(record, Mapping):
        return rejected
    version = record.get("version")
    value = record.get("value", ...)
    pending = record.get("pending_external")
    if type(version) is not int or version != CARRIER_VERSION:
        return rejected
    if value is ... or (value is not None and type(value) is not bool) or type(pending) is not bool:
        return rejected
    if pending and value is not None:
        return rejected
    if type(column) is not bool or column is not (value is True):
        return rejected
    return ArchiveFact(value, pending_external=pending, recorded=True)


def write_archive_fact(archive, value: bool | None, *, pending_external: bool = False) -> None:
    """The one writer of an archive's declaration: the bool projection and the record, together.

    Only the reserved key is replaced; every other ``extra_data`` key is kept. A new dict is assigned, so the ORM
    sees the change and no other holder of the old dict is affected. What :func:`archive_fact` would reject is
    refused here instead of being written.
    """
    if value is not None and type(value) is not bool:
        raise ValueError(f"a swap declaration is True, False or None, not {value!r}")
    if type(pending_external) is not bool or (pending_external and value is not None):
        raise ValueError("pending_external is a bool, and True only while the declaration is unknown")
    extra = getattr(archive, "extra_data", None)
    merged = dict(extra) if isinstance(extra, Mapping) else {}
    merged[CARRIER_KEY] = {"version": CARRIER_VERSION, "value": value, "pending_external": pending_external}
    archive.swap_compatible = value is True
    archive.extra_data = merged


def strip_reserved(metadata: Mapping | None) -> dict:
    """``metadata`` without the reserved key: a parsed source file must not inject a declaration."""
    return {key: item for key, item in (metadata or {}).items() if key != CARRIER_KEY}


def public_extra_data(extra: Mapping | None) -> dict | None:
    """A copy of ``extra_data`` for an API response: the record is internal, everything else is returned as is."""
    if extra is None:
        return None
    return strip_reserved(extra) if isinstance(extra, Mapping) else extra


# --- git backup (§11) -----------------------------------------------------------------------------------------------

#: The projection's key in a backed-up archive entry; the record travels under :data:`CARRIER_KEY`.
BACKUP_PROJECTION_KEY = "swap_compatible"


def backup_fields(archive) -> dict:
    """What a git backup carries of an archive's declaration: the projection, and the record when there is one.

    Nothing else of ``extra_data`` -- no dispatch or pending authority travels with the metadata.
    """
    column = getattr(archive, "swap_compatible", None)
    fields = {BACKUP_PROJECTION_KEY: column if type(column) is bool else False}
    extra = getattr(archive, "extra_data", None)
    if isinstance(extra, Mapping) and CARRIER_KEY in extra:
        fields[CARRIER_KEY] = deepcopy(extra[CARRIER_KEY])
    # The source's declaration for a future reprint, portably (T9 spec r3 §9): present only when the row has one;
    # one this version cannot read travels as an explicit null, which a restore turns into a tombstone.
    if getattr(archive, "swap_source_declaration", None) is not None:
        fields["swap_source_declaration"] = portable_declaration(archive)
    return fields


def restored_declaration(entry: Mapping, existing=None) -> tuple[bool, bool | None]:
    """``(write, value)`` for the archive a backup entry restores -- ``existing`` is the local row it overwrites.

    A record in the backup is read strictly against its projection: valid keeps its value, pending comes back
    unknown and NOT pending (the backup proves no origin now), malformed or contradicted is unknown, and an
    explicit null is unknown. A backup with no record says nothing: unknown on insert, the local row untouched on
    overwrite -- except that an explicit strict True projection brings a legacy True across onto a row without a
    record of its own.
    """
    if CARRIER_KEY in entry:
        record = entry[CARRIER_KEY]
        if record is None:
            return True, None
        probe = SimpleNamespace(swap_compatible=entry.get(BACKUP_PROJECTION_KEY), extra_data={CARRIER_KEY: record})
        return True, archive_fact(probe).value
    legacy_true = entry.get(BACKUP_PROJECTION_KEY) is True
    if existing is None:
        return True, True if legacy_true else None
    if legacy_true and not archive_fact(existing).recorded:
        return True, True
    return False, None


# --- a source's declaration for future use (T9 spec r3 §3) ---------------------------------------------------------

#: The column both sources keep it in: an archive's declaration of its SOURCE for a future reprint, apart from the
#: executed run's fact; a library row's hash-bound carrier, beside the bool projection. Read and written here only.
DECLARATION_COLUMN = "swap_source_declaration"
DECLARATION_VERSION = 1
DECLARATION_ORIGINS = frozenset({"classifier", "generated", "transferred", "operator"})
#: An archive's source can only be declared by an operator (or restored with that origin): every other origin
#: describes an ingest, which an archive's file never had.
ARCHIVE_ORIGINS = frozenset({"operator"})
ACTOR_KINDS = frozenset({"user", "api_key", "auth_disabled", "restored"})
OPERATOR_FIELDS = ("declaration_id", "declared_at", "actor_id", "actor_label", "actor_kind")
_BASE_FIELDS = frozenset({"version", "value", "sha256", "origin"})
#: What a restore writes for a declaration it cannot read (§9): present, so no fallback opens; unreadable, so unknown.
INVALID_DECLARATION = {"version": DECLARATION_VERSION, "invalid": True}


@dataclass(frozen=True)
class Declaration:
    value: bool
    sha256: str
    origin: str
    operator: Mapping | None = None


class _Unreadable:
    """A declaration that is there and cannot be read: unknown, with no fallback of any kind."""


UNREADABLE = _Unreadable()


def _valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _valid_utc(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    return moment.tzinfo is not None and moment.utcoffset().total_seconds() == 0


def _valid_operator(record: Mapping) -> bool:
    actor_id = record["actor_id"]
    return (
        _valid_uuid(record["declaration_id"])
        and _valid_utc(record["declared_at"])
        and (actor_id is None or type(actor_id) is int)
        and isinstance(record["actor_label"], str)
        and record["actor_kind"] in ACTOR_KINDS
    )


def read_declaration(record: Any, *, origins: frozenset) -> Declaration | _Unreadable | None:
    """The closed form, strictly: ``None`` when there is none, :data:`UNREADABLE` when there is one this version
    cannot trust (an unknown key, a version, a type, an origin, an operator record that does not hold), else the
    :class:`Declaration`. Extra keys grant nothing -- they make the record unreadable."""
    if record is None:
        return None
    if not isinstance(record, Mapping):
        return UNREADABLE
    origin = record.get("origin")
    expected = _BASE_FIELDS | (frozenset(OPERATOR_FIELDS) if origin == "operator" else frozenset())
    if set(record) != expected or origin not in origins:
        return UNREADABLE
    version, value, sha256 = record["version"], record["value"], record["sha256"]
    if type(version) is not int or version != DECLARATION_VERSION or type(value) is not bool or not valid_hash(sha256):
        return UNREADABLE
    if origin == "operator" and not _valid_operator(record):
        return UNREADABLE
    operator = {field: record[field] for field in OPERATOR_FIELDS} if origin == "operator" else None
    return Declaration(value=value, sha256=sha256, origin=origin, operator=operator)


def _is_library_row(row) -> bool:
    return hasattr(row, "file_hash")


def write_source_declaration(row, *, value: bool, sha256: str, origin: str, operator: Mapping | None = None) -> None:
    """The one writer of a source's declaration (a new dict). A library row gets its bool projection with it, and
    only for the bytes the row holds; an archive's executed-run fact is never touched. Refuses -- ``ValueError`` --
    anything the reader would not read back as this very declaration."""
    library = _is_library_row(row)
    record = {"version": DECLARATION_VERSION, "value": value, "sha256": sha256, "origin": origin}
    if origin == "operator":
        if not isinstance(operator, Mapping) or set(operator) != set(OPERATOR_FIELDS):
            raise ValueError("an operator's declaration carries its operator record")
        record.update({field: operator[field] for field in OPERATOR_FIELDS})
    elif operator is not None:
        raise ValueError("only an operator's declaration carries an operator record")
    if not isinstance(
        read_declaration(record, origins=DECLARATION_ORIGINS if library else ARCHIVE_ORIGINS), Declaration
    ):
        raise ValueError(f"not a declaration this version reads back: {origin!r}")
    if library and (not valid_hash(getattr(row, "file_hash", None)) or row.file_hash != sha256):
        raise ValueError("a library row's declaration is bound to the bytes the row holds")
    setattr(row, DECLARATION_COLUMN, record)
    if library:
        row.swap_compatible = value


# --- library --------------------------------------------------------------------------------------------------------


def valid_hash(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def library_fact(library_file) -> bool | None:
    """A library row's declaration for a future use -- the T9 §8 table, the SOURCE reader.

    1. A carrier that holds (its hash the row's, its value the bool projection) answers; the name never re-judges it.
    2. A carrier this version cannot trust -- or one of other bytes, or one the projection contradicts -- is unknown,
       with no fallback to the name or the bool.
    3. Without a carrier, without a valid hash: unknown (nothing ties a declaration to the bytes a job captures).
    4. Without a carrier, a strict legacy True stays True.
    5. Without a carrier, an EXTERNAL row's legacy False was the old scan's default, not a declaration: it is
       classified by the name the file has on the share -- the persisted ``file_path``, whose basename a display
       rename never changes -- exactly as the scan classifies a new file.
    6. Without a carrier, a managed row's legacy bool stands (the accepted boundary). Anything unreadable on the way
       -- the bool, ``is_external``, an empty path -- is unknown, never a plain False.
    """
    file_hash = getattr(library_file, "file_hash", None)
    legacy = getattr(library_file, "swap_compatible", None)
    declared = read_declaration(getattr(library_file, DECLARATION_COLUMN, None), origins=DECLARATION_ORIGINS)
    if isinstance(declared, _Unreadable):
        return None
    if isinstance(declared, Declaration):
        if not valid_hash(file_hash) or declared.sha256 != file_hash or legacy is not declared.value:
            return None
        return declared.value
    if not valid_hash(file_hash) or type(legacy) is not bool:
        return None
    if legacy:
        return True
    external = getattr(library_file, "is_external", None)
    if type(external) is not bool:
        return None
    if not external:
        return False
    path = getattr(library_file, "file_path", None)
    if not isinstance(path, str) or not path.strip():
        return None
    return classify_external_name(path)


def library_execution_fact(library_file) -> bool | None:
    """A library row's answer for the run that already EXECUTED from it -- historical evidence only (T9 §2, §8).

    No carrier (any one may postdate the capture: a backfill, an operator), no classification by name, no default
    False of an external row: those are what a future read may take, not what the run was captured with. Left: a
    strict legacy True, a managed row's legacy bool -- both with a valid hash. Everything else is unknown.
    """
    if getattr(library_file, DECLARATION_COLUMN, None) is not None:
        return None
    legacy = getattr(library_file, "swap_compatible", None)
    if not valid_hash(getattr(library_file, "file_hash", None)) or type(legacy) is not bool:
        return None
    if legacy:
        return True
    external = getattr(library_file, "is_external", None)
    return False if external is False else None


# --- a job's own fact: capture, snapshot, legacy (§5, §6) -----------------------------------------------------------

#: The keys of a job's ``source_snapshot`` this module owns: the fact (JSON true/false/null) and, for an unknown,
#: why -- diagnostics only, never a permission.
SNAPSHOT_FACT_KEY = "swap_compatible"
SNAPSHOT_REASON_KEY = "swap_reason"

REASON_HASH_MISSING = "hash_missing"
REASON_HASH_MISMATCH = "hash_mismatch"
REASON_SOURCE_UNKNOWN = "source_unknown"
REASON_RECORD_INVALID = "record_invalid"
UNKNOWN_REASONS = frozenset({REASON_HASH_MISSING, REASON_HASH_MISMATCH, REASON_SOURCE_UNKNOWN, REASON_RECORD_INVALID})


@dataclass(frozen=True)
class SourceFact:
    """A job's declaration as its source handed it over.

    ``reason`` says why an unknown is unknown and is ``None`` for a known value. ``missing`` is True only for a
    valid snapshot written before this fix, which has no key at all -- the one state §6's legacy resolution may
    answer; everything else, an explicit null included, is final.
    """

    value: bool | None
    reason: str | None = None
    missing: bool = False


UNKNOWN_SOURCE = SourceFact(None, REASON_SOURCE_UNKNOWN)


def _unknown(reason: str) -> SourceFact:
    return SourceFact(None, reason)


def archive_source(archive) -> tuple[SourceFact, str | None]:
    """An archive's SOURCE declaration for a future use and the hash its bytes must have for it to count.

    An operator's declaration of the source (T9 §2) answers first, bound to its own hash -- the bytes it was made
    for, whatever the historical hashes say; one this version cannot read is unknown, with no fallback to the run's
    fact. Without one, the executed run's fact (D1) and the archive's unpatched source hash (its ``content_hash`` only
    when it has none).
    """
    declared = read_declaration(getattr(archive, DECLARATION_COLUMN, None), origins=ARCHIVE_ORIGINS)
    if isinstance(declared, _Unreadable):
        return _unknown(REASON_RECORD_INVALID), None
    if isinstance(declared, Declaration):
        return SourceFact(declared.value), declared.sha256
    return _archive_run_source(archive)


def _archive_run_source(archive) -> tuple[SourceFact, str | None]:
    """The executed run's fact as a source answer, with the archive's own hash -- what an execution read may use."""
    fact = archive_fact(archive)
    expected = getattr(archive, "source_content_hash", None) or getattr(archive, "content_hash", None)
    if fact.value is None:
        return _unknown(REASON_RECORD_INVALID if fact.invalid else REASON_SOURCE_UNKNOWN), expected
    return SourceFact(fact.value), expected


def library_source(library_file) -> tuple[SourceFact, str | None]:
    """A library row's declaration and its content hash. Without a valid hash nothing ties the declaration to
    the bytes a job will capture."""
    expected = getattr(library_file, "file_hash", None)
    if not valid_hash(expected):
        return _unknown(REASON_HASH_MISSING), expected
    value = library_fact(library_file)
    return (SourceFact(value) if value is not None else _unknown(REASON_RECORD_INVALID)), expected


def confirm(fact: SourceFact, expected: Any, actual: Any) -> SourceFact:
    """The declaration counts for the captured bytes only when the hash they really have is the one the source
    row recorded for them."""
    if fact.value is None:
        return fact if fact.reason in UNKNOWN_REASONS else UNKNOWN_SOURCE
    if not valid_hash(expected):
        return _unknown(REASON_HASH_MISSING)
    if not valid_hash(actual) or expected != actual:
        return _unknown(REASON_HASH_MISMATCH)
    return fact


def snapshot_fact(payload: Mapping) -> SourceFact:
    """Read the fact out of a snapshot payload of the known version, strictly.

    No key at all is the legacy state; a bool is the fact; null is unknown with its recorded reason (an old null
    without one is ``source_unknown``); anything else is ``record_invalid``. Never a fallback of its own.
    """
    if SNAPSHOT_FACT_KEY not in payload:
        return SourceFact(None, missing=True)
    value = payload[SNAPSHOT_FACT_KEY]
    if type(value) is bool:
        return SourceFact(value)
    if value is None:
        reason = payload.get(SNAPSHOT_REASON_KEY)
        return _unknown(reason if reason in UNKNOWN_REASONS else REASON_SOURCE_UNKNOWN)
    return _unknown(REASON_RECORD_INVALID)


def snapshot_fact_malformed(payload: Mapping) -> bool:
    """Whether a snapshot of the known version carries a fact that is neither a bool nor an explicit unknown -- a
    payload no refresh may repair into a declaration (swap-source-declaration spec r3 §6)."""
    if SNAPSHOT_FACT_KEY not in payload:
        return False
    value = payload[SNAPSHOT_FACT_KEY]
    return value is not None and type(value) is not bool


def invalid_snapshot() -> SourceFact:
    """The fact of a snapshot this version cannot read: unknown, and final."""
    return _unknown(REASON_RECORD_INVALID)


def snapshot_fields(fact: SourceFact) -> dict:
    """The keys a snapshot carries for ``fact``. A still-missing legacy fact is written as an unknown: a copy
    records an answer, it never passes the question on."""
    if fact.value is not None:
        return {SNAPSHOT_FACT_KEY: fact.value}
    reason = fact.reason if fact.reason in UNKNOWN_REASONS else REASON_SOURCE_UNKNOWN
    return {SNAPSHOT_FACT_KEY: None, SNAPSHOT_REASON_KEY: reason}


def snapshot_with_fact(snapshot: Mapping, fact: SourceFact) -> dict:
    """A NEW snapshot dict for another job, carrying ``fact`` -- the parent's payload is never touched."""
    new = deepcopy(dict(snapshot))
    new.pop(SNAPSHOT_REASON_KEY, None)
    new.update(snapshot_fields(fact))
    return new


def snapshot_without_fact(snapshot):
    """The snapshot as copied to another job, ignoring only the dispatch declaration."""
    from backend.app.services.queue_source_descriptor import SOURCE_SNAPSHOT_VERSION

    if isinstance(snapshot, dict) and snapshot.get("version") == SOURCE_SNAPSHOT_VERSION:
        return {key: value for key, value in snapshot.items() if key not in {SNAPSHOT_FACT_KEY, SNAPSHOT_REASON_KEY}}
    return snapshot


def same_snapshot_for_return(parent, copy) -> bool:
    normalized = snapshot_without_fact(parent)
    if normalized != snapshot_without_fact(copy):
        return False
    if normalized is parent:
        return True  # Other payload versions are copied exactly.
    old_fact, new_fact = snapshot_fact(parent), snapshot_fact(copy)
    return old_fact.missing or (not new_fact.missing and old_fact.value == new_fact.value)


#: Who asks a pre-fix snapshot's legacy fact: a future dispatch of the job (the source readers -- an operator's
#: declaration, §8's classification), or the completion of a run that already executed (historical evidence only).
LEGACY_CONTEXTS = frozenset({"source", "execution"})


def _library_execution_source(library_file) -> tuple[SourceFact, str | None]:
    value = library_execution_fact(library_file)
    expected = getattr(library_file, "file_hash", None)
    return (SourceFact(value) if value is not None else _unknown(REASON_SOURCE_UNKNOWN)), expected


def resolve_legacy(*, context: str, provenance_kind: Any, archive, library_file, sha256: Any) -> SourceFact:
    """§6: the fact of a pre-fix snapshot, from the job's OWN live link whose hash matches the blob.

    ``context`` is required and has no default (T9 §2): ``"source"`` for a future dispatch, ``"execution"`` for the
    completion of a run already executed -- which never sees an operator's declaration nor §8's classification.
    ``archive`` / ``library_file`` are the rows behind the job's foreign keys (``None`` when the link is gone).
    The provenance's kind may narrow which link is meant; its id never authorises a lookup. Two candidate links
    that the kind does not decide between, or none, are unknown. A legacy archive's False stays unknown (D1).
    """
    if context not in LEGACY_CONTEXTS:
        raise ValueError(f"not a legacy context: {context!r}")
    future = context == "source"
    candidates = []
    if archive is not None and provenance_kind in (None, "archive"):
        candidates.append(archive_source(archive) if future else _archive_run_source(archive))
    if library_file is not None and provenance_kind in (None, "library_file"):
        candidates.append(library_source(library_file) if future else _library_execution_source(library_file))
    if len(candidates) != 1:
        return UNKNOWN_SOURCE
    fact, expected = candidates[0]
    return confirm(fact, expected, sha256)


# --- policy ---------------------------------------------------------------------------------------------------------


def _requested_events(events: Any) -> tuple[str, ...] | None:
    if not isinstance(events, (list, tuple)) or not events:
        return None
    if not all(isinstance(event, str) and event in SWAP_EVENTS for event in events):
        return None
    return tuple(events)


def effective_request(*, execute: Any, events: Any, printer_swap_mode: bool) -> bool:
    """Whether a job really asks for our swap macros on this printer: ``execute`` strictly True, a non-empty list
    of known events, and a printer in swap mode. The default ``execute=True`` with no events asks for nothing, and
    an opt-out is never raised into a request."""
    return execute is True and printer_swap_mode is True and _requested_events(events) is not None


def decide(fact: Any, *, execute: Any, events: Any, printer_swap_mode: bool) -> SwapDecision:
    """What a job may fire of OUR swap macros, given its source's declaration (§2, §8).

    - True: the source carries its own sequence -- ours are muted, request or not.
    - False: exactly what was requested, under the printer's swap mode; nothing is ever added or raised.
    - Unknown with an effective request: refused (D2), never a silent strip. Unknown without one: nothing to run.
    """
    requested = effective_request(execute=execute, events=events, printer_swap_mode=printer_swap_mode)
    known = _strict_fact(fact)
    if known is False and requested:
        return SwapDecision(True, _requested_events(events))
    if known is None and requested:
        return SwapDecision(False, None, refused=True, reason=UNKNOWN_SOURCE_REASON)
    return SwapDecision(False, None)


class SwapRequestRefused(ValueError):
    """D2: our swap macros were asked for a source whose declaration is unknown."""

    def __init__(self, reason: str = UNKNOWN_SOURCE_REASON):
        super().__init__(reason)
        self.reason = reason


def resolve_request(fact: Any, *, execute: Any, events: Any, printer_swap_mode: bool) -> tuple[bool, list[str] | None]:
    """What a NEW job stores of a request for our swap macros: ``(execute, events)``, or :class:`SwapRequestRefused`.

    The stored fields say what will run -- never a request the job cannot honour (§8).
    """
    decision = decide(fact, execute=execute, events=events, printer_swap_mode=printer_swap_mode)
    if decision.refused:
        raise SwapRequestRefused(decision.reason)
    return decision.execute, (list(decision.events) if decision.execute else None)


def project(value: Any) -> tuple[bool, bool]:
    """The wire projection: ``(swap_compatible, swap_compatibility_known)``. The bool keeps its old meaning."""
    known = _strict_fact(value)
    return known is True, known is not None


#: The answer field that says whether ``swap_compatible`` is a known declaration or the unknown's False (§9).
KNOWN_FIELD = "swap_compatibility_known"


def wire_fields(value: Any) -> dict:
    """The two answer fields of a declaration a reader returned -- every serializer spreads exactly these."""
    swap, known = project(value)
    return {SNAPSHOT_FACT_KEY: swap, KNOWN_FIELD: known}


# --- what an operator's declaration is checked against (T9 spec r3 §5) -----------------------------------------------


def source_effective(row) -> bool | None:
    """The source fact a future use of this row would get: the archive's source reader, the library's §8 table."""
    if _is_library_row(row):
        return library_fact(row)
    return archive_source(row)[0].value


def current_declaration(row) -> Declaration | None:
    """The row's readable declaration, or ``None`` (none, or one this version cannot read)."""
    origins = DECLARATION_ORIGINS if _is_library_row(row) else ARCHIVE_ORIGINS
    declared = read_declaration(getattr(row, DECLARATION_COLUMN, None), origins=origins)
    return declared if isinstance(declared, Declaration) else None


def source_revision(row, *, epoch: int) -> str:
    """An optimistic equality token over the facts a declaration is made about (§5) -- never an authority.

    The kind and id, the stored path and the hashes, the current declaration (with its id: a second declaration of
    the same value is another revision), an archive's executed-run fact and its status, the bool projection, the
    owner, trashed, and the storage generation. Notes, energy and the rest of ``extra_data`` are not part of it.
    """
    import hashlib
    import json

    library = _is_library_row(row)
    run = None
    if not library:
        extra = getattr(row, "extra_data", None)
        run = extra.get(CARRIER_KEY) if isinstance(extra, Mapping) else None
    facts = {
        "kind": "library" if library else "archive",
        "id": getattr(row, "id", None),
        "file_path": getattr(row, "file_path", None),
        "hashes": (
            [getattr(row, "file_hash", None)]
            if library
            else [getattr(row, "source_content_hash", None), getattr(row, "content_hash", None)]
        ),
        "is_external": getattr(row, "is_external", None) if library else None,
        "declaration": getattr(row, DECLARATION_COLUMN, None),
        "run": run,
        "projection": getattr(row, "swap_compatible", None),
        "status": None if library else getattr(row, "status", None),
        "owner": getattr(row, "created_by_id", None),
        "trashed": getattr(row, "deleted_at", None) is not None,
        "epoch": epoch,
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# --- what a library door writes (T9 spec r3 §8, plan T9.3) -----------------------------------------------------------

INGEST_ORIGINS = frozenset({"classifier", "generated", "transferred"})


def declaration_columns(value: bool, sha256: str, origin: str) -> dict:
    """The two columns a library declaration is, for a writer that updates by statement (the scan): the carrier
    bound to ``sha256`` and its bool projection, never one without the other."""
    if origin not in INGEST_ORIGINS:
        raise ValueError(f"not an ingest origin: {origin!r}")
    record = {"version": DECLARATION_VERSION, "value": value, "sha256": sha256, "origin": origin}
    if not isinstance(read_declaration(record, origins=DECLARATION_ORIGINS), Declaration):
        raise ValueError("not a declaration this version reads back")
    return {"swap_compatible": value, DECLARATION_COLUMN: record}


def declare_ingest(row, value: bool, *, origin: str) -> None:
    """A library door's declaration of the bytes it just wrote: the hash-bound carrier with its projection -- or,
    for a row without a valid hash (nothing to bind a carrier to), the projection alone, which the reader answers
    unknown until a scan gives the row its hash."""
    if origin not in INGEST_ORIGINS:
        raise ValueError(f"not an ingest origin: {origin!r}")
    if valid_hash(getattr(row, "file_hash", None)):
        write_source_declaration(row, value=value, sha256=row.file_hash, origin=origin)
        return
    row.swap_compatible = value is True
    setattr(row, DECLARATION_COLUMN, None)


def scan_declaration(row, *, new_hash: str, new_identity: bool, entry_name: str | None) -> dict:
    """What a scan's refresh of ``row`` writes of its declaration, read from the row as it is NOW (locked):

    - new bytes on the path: a new classifier carrier by the share's entry name, with the new hash -- whatever was
      declared for the old bytes (an operator's declaration included) is not carried over;
    - the repair of a row that had no valid hash: an external row without a carrier whose bool is the old default
      False is classified by its persisted name on the share (§8, C) and bound to the actual hash;
    - the same bytes: nothing -- an operator's declaration, any carrier, the bool all stay as they are.
    """
    if new_identity:
        return declaration_columns(classify_external_name(entry_name), new_hash, "classifier")
    repaired = not valid_hash(getattr(row, "file_hash", None)) and valid_hash(new_hash)
    if (
        repaired
        and getattr(row, DECLARATION_COLUMN, None) is None
        and getattr(row, "is_external", None) is True
        and getattr(row, "swap_compatible", None) is False
        and isinstance(getattr(row, "file_path", None), str)
        and row.file_path.strip()
    ):
        return declaration_columns(classify_external_name(row.file_path), new_hash, "classifier")
    return {}


# --- a job's incarnation (T9 spec r3 §3, §6) --------------------------------------------------------------------------

#: The column naming one incarnation of a job: a reused integer id under a new value is another job.
JOB_IDENTITY_COLUMN = "swap_declaration_identity"


def job_identity(job) -> str | None:
    """The job's incarnation, read in this one place -- what a refresh's revision and a claim's basis compare."""
    return getattr(job, JOB_IDENTITY_COLUMN, None)


# --- a source's declaration on the wire and in a git backup (T9 spec r3 §7, §9) -------------------------------------

#: What travels of an operator's declaration, on the wire and in a backup: never the local user id -- on another
#: farm it names someone else, and here it is nobody's business.
PORTABLE_FIELDS = ("version", "value", "sha256", "origin", "declaration_id", "declared_at", "actor_label", "actor_kind")


def portable_declaration(row) -> dict | None:
    """A source's current declaration without the local actor id, or ``None`` when there is none it can read."""
    declaration = current_declaration(row)
    if declaration is None:
        return None
    record = getattr(row, DECLARATION_COLUMN)
    return {field: record[field] for field in PORTABLE_FIELDS if field in record}


def source_wire_fields(archive) -> dict:
    """The archive answer's SOURCE pair -- what a future reprint reads (the operator's declaration first) -- and
    the declaration's metadata, beside the historical pair of the executed run; neither falls back to the other."""
    fact, _expected = archive_source(archive)
    swap, known = project(fact.value)
    return {
        "source_swap_compatible": swap,
        "source_swap_compatibility_known": known,
        "source_swap_declaration": portable_declaration(archive),
    }


def restored_source_declaration(entry: Mapping, existing=None) -> tuple[bool, dict | None]:
    """``(write, record)`` for an archive's source declaration a git backup entry restores (§9).

    No field in the backup says nothing: nothing on insert, the local declaration kept on overwrite. A portable
    operator record that reads back becomes a RESTORED one -- its id, value, hash, time and actor label kept, no local
    user id, ``actor_kind`` "restored" -- so a restore is never passed off as a local operator's confirmation. Anything
    else -- an explicit null, a malformed or foreign payload -- is the tombstone: present, so no fallback opens, and
    unreadable, so unknown."""
    if DECLARATION_COLUMN not in entry:
        return False, None
    carried = entry[DECLARATION_COLUMN]
    if isinstance(carried, Mapping) and set(carried) == set(PORTABLE_FIELDS) and carried.get("origin") == "operator":
        record = {
            "version": carried["version"],
            "value": carried["value"],
            "sha256": carried["sha256"],
            "origin": "operator",
            "declaration_id": carried["declaration_id"],
            "declared_at": carried["declared_at"],
            "actor_id": None,
            "actor_label": carried["actor_label"],
            "actor_kind": "restored",
        }
        if isinstance(read_declaration(record, origins=ARCHIVE_ORIGINS), Declaration):
            return True, record
    return True, dict(INVALID_DECLARATION)


def restore_source_declaration(archive, record: dict) -> None:
    """The restore's writer of an archive's source declaration: a restored record or the tombstone, nothing else."""
    if record != INVALID_DECLARATION:
        declaration = read_declaration(record, origins=ARCHIVE_ORIGINS)
        if not isinstance(declaration, Declaration) or declaration.operator["actor_kind"] != "restored":
            raise ValueError("not a restored declaration")
    setattr(archive, DECLARATION_COLUMN, dict(record))
