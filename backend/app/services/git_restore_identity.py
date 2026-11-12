"""Archive identity planning and DB lifetime locks for metadata restore.

No filename/hash lookup can become a fallback match for a supplied UUID.
Only the caller's category owner commits or rolls back these locks/writes.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select

from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services.archive_write_scope import lock_prints
from backend.app.services.git_restore_fields import INVALID, MISSING, FieldRule, read_field, source_id
from backend.app.services.git_restore_records import (
    ARCHIVE_FIELDS,
    ArchiveProof,
    hash_value,
    parsed_fields,
    unknown_outcome,
    uuid_value,
)


class ArchiveIdentityConflict(ValueError):
    pass


@dataclass
class ArchiveInput:
    entry: dict
    values: dict
    identity: str | None
    content_hash: object
    source_hash: object
    unknown: bool

    @property
    def started_at(self):
        value = self.values["started_at"]
        return value if isinstance(value, datetime) else None


def parse_archive(entry, date_parser):
    values = parsed_fields(entry, ARCHIVE_FIELDS, date_parser)
    uncertain = read_field(entry, "recovered_outcome_uncertain", FieldRule("bool"), date_parser)
    return ArchiveInput(
        entry,
        values,
        uuid_value(entry.get("recovery_identity", MISSING)),
        hash_value(entry.get("content_hash", MISSING)),
        hash_value(entry.get("source_content_hash", MISSING)),
        unknown_outcome(values, uncertain),
    )


def valid_serial(value):
    parsed = read_field({"v": value}, "v", FieldRule("str", 50, nonempty=True), lambda _: None)
    return parsed if isinstance(parsed, str) and parsed else None


def printer_for(entry, printers):
    key = source_id(entry.get("printer_id"))
    serial = valid_serial(entry.get("printer_serial"))
    return key if key is not INVALID and serial is not None and printers.get(key) == serial else None


async def lock_printers(db, ids):
    ids = sorted({i for i in ids if source_id(i) is not INVALID})
    if not ids:
        return {}
    rows = await db.execute(
        select(Printer.id, Printer.serial_number)
        .where(Printer.id.in_(ids))
        .order_by(Printer.id)
        .with_for_update(read=True, key_share=True)
    )
    return {key: serial for key, serial in rows if valid_serial(serial) is not None}


async def lock_owners(db, records):
    names, ids = set(), set()
    for record in records:
        username = read_field(record.entry, "created_by_username", FieldRule("str", 100), lambda _: None)
        if isinstance(username, str) and username:
            names.add(username)
        elif username is MISSING or username is None or username == "":
            key = source_id(record.entry.get("created_by_id"))
            if key is not INVALID:
                ids.add(key)
    if not names and not ids:
        return {}, set()
    rows = (
        await db.execute(
            select(User.id, User.username)
            .where(or_(User.id.in_(ids), User.username.in_(names)))
            .order_by(User.id)
            .with_for_update(read=True, key_share=True)
        )
    ).all()
    return {name: key for key, name in rows}, {key for key, _ in rows}


def _start_filter(db, started_at):
    if started_at is None:
        return PrintArchive.started_at.is_(None)
    if db.get_bind().dialect.name == "sqlite":
        # Coarse SQL prefilter only. Exact Python comparison below also handles
        # legacy second-precision values bound without fractional text digits.
        return func.strftime("%Y-%m-%d %H:%M:%S", PrintArchive.started_at) == started_at.isoformat(
            sep=" ", timespec="seconds"
        )
    return PrintArchive.started_at == started_at


async def candidates(db, record):
    if record.identity is INVALID:
        return []
    statement = select(PrintArchive).execution_options(populate_existing=True)
    if record.identity is not None:
        rows = list((await db.scalars(statement.where(PrintArchive.recovery_identity == record.identity))).all())
        if rows:
            return rows
    clauses = []
    if isinstance(record.content_hash, str):
        clauses.append(PrintArchive.content_hash == record.content_hash)
    if isinstance(record.values["filename"], str) and record.started_at is not None:
        clauses.append(PrintArchive.filename == record.values["filename"])
    key = source_id(record.entry.get("printer_id"))
    if key is not INVALID and record.started_at is not None:
        clauses.append(PrintArchive.printer_id == key)
    if not clauses:
        return []
    rows = await db.scalars(statement.where(or_(*clauses), _start_filter(db, record.started_at)))
    return [row for row in rows if row.started_at == record.started_at]


def resolve(record, rows, printers):
    if record.identity is INVALID:
        raise ArchiveIdentityConflict("invalidUUID")
    if record.identity is None and record.unknown:
        raise ArchiveIdentityConflict("legacyUnknownOutcome")
    incoming_printer = printer_for(record.entry, printers)
    if record.identity is not None:
        exact = [row for row in rows if row.recovery_identity == record.identity]
        if len(exact) > 1:
            raise ArchiveIdentityConflict("ambiguousUUID")
        if exact:
            return exact[0]
        # Weak keys are conflict detectors only, including on NULL/unproven
        # printer rows left by a legacy restore which minted its own UUID.
        for row in rows:
            weak_key = (
                isinstance(record.content_hash, str)
                and row.content_hash == record.content_hash
                or record.started_at is not None
                and row.filename == record.values["filename"]
            )
            same_printer = incoming_printer is not None and row.printer_id == incoming_printer
            if not weak_key and not same_printer:
                continue
            other_printer = (
                row.printer_id in printers and incoming_printer is not None and row.printer_id != incoming_printer
            )
            if not other_printer:
                raise ArchiveIdentityConflict("differentUUID")
        if record.unknown and (incoming_printer is None or record.started_at is None):
            raise ArchiveIdentityConflict("unknownWithoutPrinterStart")
        return None
    # Legacy terminal compatibility: evaluate each key, never first(), and
    # never attribute a raw printer ID that this instance cannot prove.
    scoped = [r for r in rows if incoming_printer is None or r.printer_id == incoming_printer]
    hashes = [r for r in scoped if isinstance(record.content_hash, str) and r.content_hash == record.content_hash]
    names = [r for r in scoped if record.started_at is not None and r.filename == record.values["filename"]]
    if len(hashes) > 1 or len(names) > 1:
        raise ArchiveIdentityConflict("ambiguousLegacy")
    found = {r.id: r for r in hashes + names}
    if len(found) > 1:
        raise ArchiveIdentityConflict("contradictoryLegacy")
    if found:
        return next(iter(found.values()))
    if incoming_printer is not None and any(r.printer_id == incoming_printer for r in rows):
        raise ArchiveIdentityConflict("legacyRunConflict")
    if not isinstance(record.content_hash, str) and not (
        record.started_at is not None and isinstance(record.values["filename"], str)
    ):
        raise ArchiveIdentityConflict("missingLegacyKey")
    return None


async def prepare(db, records):
    """Plan without writes, lock in owner/printer/print order; caller rereads."""
    planned, printer_ids = set(), set()
    with db.no_autoflush:
        for record in records:
            rows = await candidates(db, record)
            planned.update(row.id for row in rows)
            printer_ids.update(row.printer_id for row in rows)
            printer_ids.add(record.entry.get("printer_id") if type(record.entry.get("printer_id")) is int else None)
    owners = await lock_owners(db, records)
    printers = await lock_printers(db, printer_ids)
    if planned:
        await lock_prints(db, planned)
    return planned, printers, owners


async def usage_basis(db, usage, id_map, proof_map):
    """Revalidate identities after the archive category released its locks."""
    expected = {
        old: proof
        for old, proof in (proof_map or {}).items()
        if source_id(old) is not INVALID
        and isinstance(proof, ArchiveProof)
        and id_map.get(old) == proof.local_id
        and isinstance(proof.identity, str)
    }
    printer_ids = {proof.printer_id for proof in expected.values()}
    printer_ids.update(
        e.get("printer_id") for e in usage if isinstance(e, dict) and source_id(e.get("printer_id")) is not INVALID
    )
    printers = await lock_printers(db, printer_ids)
    if not expected:
        return printers, {}
    rows = (
        await db.execute(
            select(PrintArchive.id, PrintArchive.recovery_identity, PrintArchive.printer_id)
            .where(PrintArchive.id.in_(sorted({p.local_id for p in expected.values()})))
            .order_by(PrintArchive.id)
            .with_for_update(read=True, key_share=True)
        )
    ).all()
    current = {key: (identity, printer) for key, identity, printer in rows}
    valid = {}
    for old, proof in expected.items():
        identity, printer = current.get(proof.local_id, (None, None))
        if uuid_value(identity) == proof.identity:
            valid[old] = ArchiveProof(proof.local_id, proof.identity, printer)
    return printers, valid
