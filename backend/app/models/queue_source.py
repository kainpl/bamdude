"""One managed copy of the bytes a queued job prints — spec §4.

Spec: ``60-specs/queue-source-spool-spec.md`` (the vault note; §4 is the file
and data model). A job's independence from the laptop, the SMB share, the
library row and the archive is this table plus the file it names under
``settings.data_dir / "queue-spool"``.

**One row per distinct content, never per job.** The same 3MF queued a hundred
times across both tiers, several plates, several printers and several people is
one row and one file (A01); ownership, the human filename, the order line, the
plate and the print options belong to the *job*, not here (§4). The key is the
SHA-256 of the bytes that were actually captured — never the path, the library
id, the batch or a hash computed earlier by somebody else (S4), which is why
``sha256`` is UNIQUE and a second claim on it is a refusal the writer has to
handle rather than a 500 (A07).

**There is deliberately no ``ref_count`` column.** The owners are every job row
in either queue that names this id — whatever its status — plus the short-lived
pins a capture, a dispatch or a backup holds (§9). A counter would have to be
rebuilt from events, and the one thing worse than a slow "who still owns this"
query is a fast wrong answer that unlinks a file a pending job is about to
print (S5). ``unreferenced_at`` is only the GC's hint about when the last owner
was *last seen* to have let go; it is never on its own proof that nobody owns
the row.

**``state`` has exactly three values**, and none of them is ``preparing``: a
half-written capture lives in ``queue-spool/staging/<token>.part`` and has no
row at all until it is renamed into place, so "being prepared" is not a state
this table can be in (§4, §5). ``preparing`` is an API word only, derived from
the live supervisor — see
:data:`~backend.app.services.queue_source_descriptor.SOURCE_STORAGE_STATES`.

The four CHECKs are named so that :func:`db_portable._reconcile_check_constraints`
can add them to a database that was imported from a file which never had them —
that pass matches by name, and an unnamed CHECK is silently skipped.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

#: The verified container format. The extension on disk follows it, because the
#: raw-gcode exemption downstream branches on the format (§4) — a source whose
#: format we could not verify is not a source.
FORMAT_3MF = "3mf"
FORMAT_GCODE = "gcode"
QUEUE_SOURCE_FORMATS: tuple[str, ...] = (FORMAT_3MF, FORMAT_GCODE)

#: ``ready`` — published, hashed, fsync'd, safe to print and to reuse.
#: ``deleting`` — the GC owns it; nothing may attach to it (§9).
#: ``broken`` — the bytes on disk no longer match ``sha256``, or went missing.
#: A broken row is NOT evicted while a job still references it: the reference is
#: what lets the operator see which jobs died with it (§9).
STATE_READY = "ready"
STATE_DELETING = "deleting"
STATE_BROKEN = "broken"
QUEUE_SOURCE_STATES: tuple[str, ...] = (STATE_READY, STATE_DELETING, STATE_BROKEN)


class QueueSource(Base):
    __tablename__ = "queue_sources"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_queue_sources_sha256"),
        # Spec §4 says the hash is non-empty, and NOT NULL alone admits ``''`` and
        # a truncated value. A hex SHA-256 is 64 characters, always — so the
        # length is a fact the column can hold, and here is the one place it CAN
        # be held: SQLite cannot add a CHECK to an existing table without
        # rebuilding it, so a constraint written after m173 shipped would only
        # ever reach fresh installs. ``length()`` (not a regex) because it is the
        # one string function both dialects spell the same way.
        CheckConstraint("length(sha256) = 64", name="ck_queue_sources_sha256_length"),
        # ``>= 0``, not ``> 0``. Refusing an empty capture needs the format and
        # the CRC to decide and belongs to the writer; the column's job is only
        # to make a negative size — which could only come from an arithmetic
        # bug — impossible to store.
        CheckConstraint("size_bytes >= 0", name="ck_queue_sources_size_bytes_non_negative"),
        CheckConstraint("format IN ('3mf', 'gcode')", name="ck_queue_sources_format"),
        CheckConstraint("state IN ('ready', 'deleting', 'broken')", name="ck_queue_sources_state"),
        # The GC's only scan: candidates are the rows in a terminal-ish state
        # ordered by when they were last seen unowned (§9, grace before unlink).
        Index("ix_queue_sources_state_unreferenced_at", "state", "unreferenced_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: SHA-256 of the captured bytes, lowercase hex. The dedup key and the only
    #: proof that two jobs print the same thing (S4).
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Server path relative to ``settings.base_dir``, as every other stored path
    #: in this codebase is. A path from a client is never accepted (§4).
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=STATE_READY, server_default=STATE_READY)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    #: When the last owner was last seen to have let go. A hint for the GC's
    #: grace window, never on its own a licence to unlink (§9).
    unreferenced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
