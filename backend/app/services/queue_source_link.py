"""One rule restores or copies a job's source link, and it names a proven incarnation or nothing (G03 B, A27).

A job's ``archive_id`` / ``library_file_id`` is navigation: the bytes it prints are its own captured copy
(``queue_source_id`` — inv-queue-job-owns-its-bytes). Every writer that sets that link from a stored record — Retry,
bulk Retry, Unskip, Clone, plate Repeat, interrupted dispatch and the deferral — goes through this module, never
an id read off a record and written back as it is: SQLite gives a deleted archive's or library row's id to the
next row, and a link written because an id happened to exist names somebody else's file.

1. *The candidate* (:func:`candidate_of`): for a job that owns its bytes, the snapshot's provenance — the row its
   bytes were captured from, with the incarnation the capture stamped (:func:`stamp`); for a legacy job, the
   routing intent's ``{kind, id}``, else the row's live link.
2. *An unchanged live link needs nothing* (:func:`live_link_names`): a live link is current — every purge nulls the
   rows that name its source — so it names the incarnation it was set to, and an unchanged key takes no
   foreign-key check. A clone's copy is an insert and always checks its key: its parent is locked even then, and
   the copy carries the source row's live link when that parent is still there (and, for a stamped capture, is
   its incarnation).
3. *Otherwise the parent is locked first and re-read* (:func:`lock_parent`, ``FOR KEY SHARE``, in its class's place
   — always before the job row), and the candidate is derived again under the row's lock by the caller.
4. *The incarnation* (:func:`incarnation_of`): an archive's or a library row's ``recovery_identity`` (m204, m250),
   a random identity of the row written once when it is created; a rename on a share or a re-slice in place keeps
   it, a new row under the old id does not.
5. *The write* (:func:`link_for`): proven → the candidate's column, the other NULL; not proven for a job that owns
   its bytes → both NULL (it prints its own bytes); a legacy job's original → restored when present, NULL when
   missing. Never the job's own execution archive.
6. *Old data*: a snapshot captured before the stamp has no incarnation, so it keeps its link only by rule 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from backend.app.models.print_queue import PrintQueueItem

ARCHIVE = "archive"
LIBRARY_FILE = "library_file"
# The routing intent spells the library kind ``library``; provenance spells it ``library_file``.
_INTENT_KINDS = {"archive": ARCHIVE, "library": LIBRARY_FILE}
_PROVENANCE_KINDS = {ARCHIVE, LIBRARY_FILE}


@dataclass(frozen=True)
class Candidate:
    """The original a job's link may name: its kind, its id and — for captured bytes — the stamped incarnation."""

    kind: str
    id: int
    incarnation: str | None
    owns_bytes: bool


def incarnation_of(row) -> str | None:
    """The one computation of a source row's incarnation, for the stamp and for the test."""
    value = getattr(row, "recovery_identity", None) if row is not None else None
    return value if isinstance(value, str) and value else None


def stamp(provenance: dict, row) -> dict:
    """The capture's provenance with the incarnation of the row it was captured from (``plan_capture``)."""
    incarnation = incarnation_of(row)
    return {**provenance, "incarnation": incarnation} if incarnation is not None else dict(provenance)


def _intent_identity(item) -> tuple[str, int] | None:
    raw = getattr(item, "filament_routing", None)
    try:
        intent = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    source = intent.get("source_identity") if isinstance(intent, dict) else None
    if not isinstance(source, dict):
        return None
    kind = _INTENT_KINDS.get(source.get("kind"))
    source_id = source.get("id")
    if kind is None or type(source_id) is not int:
        return None
    return kind, source_id


def candidate_of(item, *, live_fallback: bool = True) -> Candidate | None:
    """Rule 1: the original this job's link may name, from its own immutable record."""
    if getattr(item, "queue_source_id", None) is not None:
        snapshot = item.source_snapshot if isinstance(getattr(item, "source_snapshot", None), dict) else {}
        provenance = snapshot.get("provenance") if isinstance(snapshot.get("provenance"), dict) else {}
        kind, source_id = provenance.get("kind"), provenance.get("id")
        if kind not in _PROVENANCE_KINDS or type(source_id) is not int:
            return None
        incarnation = provenance.get("incarnation")
        return Candidate(kind, source_id, incarnation if isinstance(incarnation, str) and incarnation else None, True)
    identity = _intent_identity(item)
    if identity is None and live_fallback:
        # A legacy row's library file is its source; an archive beside it is the execution archive of the print
        # that ran from it. Only a row with nothing but an archive (a print picked up from the printer) is sourced
        # from that archive.
        if getattr(item, "library_file_id", None) is not None:
            identity = (LIBRARY_FILE, item.library_file_id)
        elif getattr(item, "archive_id", None) is not None:
            identity = (ARCHIVE, item.archive_id)
    if identity is None:
        return None
    return Candidate(identity[0], identity[1], None, False)


def live_link_names(item, candidate: Candidate | None) -> bool:
    """Rule 2: the row's live link already names the candidate (so it stays, and its key takes no check)."""
    if candidate is None:
        return False
    if candidate.kind == ARCHIVE:
        return item.archive_id == candidate.id and item.library_file_id is None
    return item.library_file_id == candidate.id and item.archive_id is None


def parent_model(candidate: Candidate):
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile

    return PrintArchive if candidate.kind == ARCHIVE else LibraryFile


async def lock_parent(db, candidate: Candidate | None):
    """Rule 3: the candidate's row, locked ``FOR KEY SHARE`` and re-read (``populate_existing``); None when gone.

    ``FOR KEY SHARE`` is the lock the link's own foreign-key check would take after the job row; taken here, in the
    parent's class's place before the job row, the check then finds a lock its transaction already holds (L6(d)).
    """
    if candidate is None:
        return None
    model = parent_model(candidate)
    return await db.scalar(
        select(model)
        .where(model.id == candidate.id)
        .with_for_update(read=True, key_share=True)
        .execution_options(populate_existing=True)
    )


def proven(candidate: Candidate, parent) -> bool:
    """Rule 4: the parent read under its lock is the candidate's incarnation."""
    if parent is None:
        return False
    if not candidate.owns_bytes:
        return True  # a legacy record has only the id; its dispatch's revision check refuses another file
    return candidate.incarnation is not None and incarnation_of(parent) == candidate.incarnation


def link_for(
    candidate: Candidate | None, parent, *, execution_archive_id: int | None = None
) -> tuple[int | None, int | None]:
    """Rule 5: ``(archive_id, library_file_id)`` to write — the proven candidate, or nothing."""
    if candidate is None or not proven(candidate, parent):
        return None, None
    if candidate.kind == ARCHIVE:
        if execution_archive_id is not None and candidate.id == execution_archive_id:
            return None, None  # never the job's own execution archive
        return candidate.id, None
    return None, candidate.id


# ---------------------------------------------------------------- the doors: plan before the guards, lock, apply


@dataclass
class LinkPlan:
    """A door's plan for one row's link, made from its read before the guards (rule 3)."""

    candidate: Candidate | None
    needs_parent: bool
    parent: object = None


def plan_link(item, *, insert: bool = False) -> LinkPlan:
    """Rule 2 decides whether the parent must be locked: an unchanged live link needs nothing, except for an
    insert (a clone), whose key is always checked."""
    candidate = candidate_of(item)
    needs = candidate is not None and (insert or not live_link_names(item, candidate))
    return LinkPlan(candidate, needs)


async def lock_plans(db, plans) -> None:
    """Every planned parent ``FOR KEY SHARE``, before the job rows: library rows first, then archives, each class
    ascending — one order for every door (L5, L6(d))."""
    wanted = {(plan.candidate.kind, plan.candidate.id) for plan in plans if plan.needs_parent}
    rows = {}
    for kind, source_id in sorted(wanted, key=lambda key: (0 if key[0] == LIBRARY_FILE else 1, key[1])):
        rows[(kind, source_id)] = await lock_parent(db, Candidate(kind, source_id, None, False))
    for plan in plans:
        if plan.needs_parent:
            plan.parent = rows[(plan.candidate.kind, plan.candidate.id)]


def apply_link(target: PrintQueueItem, plan: LinkPlan, *, source=None, execution_archive_id: int | None = None) -> bool:
    """Write the link the rule allows onto ``target`` (the re-armed row, or a clone of ``source``).

    The candidate is derived again from the row read under its lock (``source`` for a clone); a different one —
    or a live link that changed after a plan that needed no parent — is the door's "changed" answer: False, and
    nothing is written.
    """
    row = source if source is not None else target
    if candidate_of(row) != plan.candidate:
        return False
    if plan.candidate is None:
        if getattr(row, "queue_source_id", None) is not None:
            target.archive_id, target.library_file_id = None, None  # nothing names an original
        return True
    if not plan.needs_parent:
        if not live_link_names(row, plan.candidate):
            return False
        if (
            execution_archive_id is not None
            and plan.candidate.kind == ARCHIVE
            and plan.candidate.id == execution_archive_id
        ):
            target.archive_id = None
        return True
    if live_link_names(row, plan.candidate) and _live_parent(plan.candidate, plan.parent):
        # Only an insert (a clone) gets here with a live link: its key is always checked, so its parent was taken
        # although the source row's live link names it (rule 2). The live link is current and the parent under its
        # lock is the row it names -- copied as it is, never as the job's own execution archive.
        if plan.candidate.kind == LIBRARY_FILE:
            target.archive_id, target.library_file_id = None, plan.candidate.id
        elif plan.candidate.id != execution_archive_id:
            target.archive_id, target.library_file_id = plan.candidate.id, None
        else:
            target.archive_id, target.library_file_id = None, None
        return True
    target.archive_id, target.library_file_id = link_for(
        plan.candidate, plan.parent, execution_archive_id=execution_archive_id
    )
    return True


def _live_parent(candidate: Candidate, parent) -> bool:
    """The parent a live link names, found under its lock; a stamped capture must also be its incarnation."""
    if parent is None:
        return False
    return candidate.incarnation is None or incarnation_of(parent) == candidate.incarnation
