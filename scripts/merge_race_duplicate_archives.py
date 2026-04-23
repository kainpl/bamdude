"""One-shot cleanup: merge race-duplicated ``print_archives`` rows.

Background
----------
Before the "Option B" dispatch refactor, two orchestrators could create an
archive for the same physical print if their timing collided:

* ``backend/app/services/background_dispatch.py::_run_print_library_file``
* ``backend/app/services/print_scheduler.py::_dispatch_item`` (library-file branch)

Classic signature of such a race dup:

* same ``printer_id`` + ``content_hash`` (so ``archive_print`` dedupped the
  file on disk → both rows point at the same ``file_path``),
* created within ≤60 seconds of each other,
* asymmetric ``library_file_id`` — one row has it, the other is NULL
  (scheduler omitted the arg; dispatcher passed it).

One of the two rows ends up referenced by the ``print_queue.archive_id``
of the queue item that caused it; that one receives the eventual
``on_print_start`` hash-adoption → ``status='printing'`` + ``started_at``.
The other is an orphan — typically ``status='archived'`` with the real
project/user/lib info that never carried over.

Strategy
--------
Pick a **survivor** per pair using this order:

1. Row referenced by a ``print_queue`` row (that's what the runtime tracks).
2. Row with ``status`` further along the lifecycle (completed > failed/cancelled
   > printing > archived).
3. Row with ``started_at`` set.
4. Newer row.

Backfill every null-but-present field from the orphan into the survivor
(``library_file_id``, ``project_id``, ``created_by_id``,
``source_content_hash``, ``applied_patches``, ``started_at``,
``completed_at``, ``cost``, ``quantity``, ``energy_*``, ``thumbnail_path``,
``timelapse_path``, ``failure_reason``, ``notes``, ``tags``, ``is_favorite``,
``photos``, ``makerworld_url``, ``designer``, ``external_url``,
``subtask_id``). ``extra_data`` is JSON-merged (survivor wins on conflict).

Advance ``status`` to the further-along value when the orphan's status is
more progressed.

Re-point any dependent rows (``print_queue.archive_id``) that still pointed
at the orphan to the survivor; delete the orphan row.

Usage
-----
.. code-block:: bash

    # See what would happen (default):
    python scripts/merge_race_duplicate_archives.py

    # Apply the merge:
    python scripts/merge_race_duplicate_archives.py --apply

    # Loose mode — include pairs that have different file_path but look
    # suspicious (e.g. both lib_id set but different, or archived/printing
    # pair for same content_hash within 5 minutes). Still requires --apply
    # to actually write.
    python scripts/merge_race_duplicate_archives.py --loose

The strict signature catches race dups with very high confidence. Loose
mode is an assist for manual review and prints candidates without touching
the DB unless you also pass ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Status ranking — higher = further along the lifecycle.
_STATUS_RANK: dict[str, int] = {
    "archived": 0,
    "pending": 1,
    "printing": 2,
    "cancelled": 3,
    "aborted": 3,
    "stopped": 3,
    "failed": 4,
    "completed": 5,
}

_FIELDS_BACKFILL_FROM_ORPHAN: tuple[str, ...] = (
    "library_file_id",
    "project_id",
    "created_by_id",
    "source_content_hash",
    "applied_patches",
    "subtask_id",
    "started_at",
    "completed_at",
    "cost",
    "energy_kwh",
    "energy_cost",
    "energy_start_kwh",
    "thumbnail_path",
    "timelapse_path",
    "source_3mf_path",
    "f3d_path",
    "failure_reason",
    "makerworld_url",
    "designer",
    "external_url",
    "notes",
    "tags",
    "photos",
    "is_favorite",
)


def _status_rank(status: str | None) -> int:
    return _STATUS_RANK.get(status or "", -1)


def _pick_survivor(
    a: sqlite3.Row,
    b: sqlite3.Row,
    queue_referenced_ids: set[int],
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Return (survivor, orphan) per the tiebreakers documented above."""
    candidates = [a, b]

    # 1. Queue-item-referenced row wins.
    queue_refs = [r for r in candidates if r["id"] in queue_referenced_ids]
    if len(queue_refs) == 1:
        survivor = queue_refs[0]
        orphan = a if survivor is b else b
        return survivor, orphan

    # 2. Status further along lifecycle wins.
    a_rank = _status_rank(a["status"])
    b_rank = _status_rank(b["status"])
    if a_rank != b_rank:
        return (a, b) if a_rank > b_rank else (b, a)

    # 3. Row with started_at set wins over one without.
    if (a["started_at"] is None) != (b["started_at"] is None):
        return (a, b) if a["started_at"] is not None else (b, a)

    # 4. Newer row wins (later id).
    return (a, b) if a["id"] > b["id"] else (b, a)


def _merge_extra_data(survivor_raw: str | None, orphan_raw: str | None) -> str | None:
    """Shallow-merge two JSON dicts; survivor wins on conflict."""
    s = _safe_json_obj(survivor_raw)
    o = _safe_json_obj(orphan_raw)
    if s is None and o is None:
        return survivor_raw if survivor_raw is not None else orphan_raw
    if s is None:
        return orphan_raw
    if o is None:
        return survivor_raw
    merged = {**o, **s}  # survivor's keys overwrite orphan's on conflict
    return json.dumps(merged, ensure_ascii=False)


def _safe_json_obj(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _earliest(a: Any, b: Any) -> Any:
    """Smaller (earlier) datetime string, for DATETIME columns stored as text."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def _latest(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _merge_fields(
    survivor: sqlite3.Row,
    orphan: sqlite3.Row,
) -> dict[str, Any]:
    """Build a dict of field→value updates to apply to the survivor row.

    Only non-null orphan values that the survivor doesn't carry are taken.
    For ``started_at`` we prefer the *earliest* non-null, for ``completed_at``
    the *latest* non-null (so the timespan is maximised).
    """
    updates: dict[str, Any] = {}
    for field in _FIELDS_BACKFILL_FROM_ORPHAN:
        s_val = survivor[field]
        o_val = orphan[field]
        if o_val is None:
            continue
        if field == "started_at":
            new = _earliest(s_val, o_val)
        elif field == "completed_at":
            new = _latest(s_val, o_val)
        else:
            if s_val is not None:
                continue
            new = o_val
        if new != s_val:
            updates[field] = new

    # Advance status if orphan's is further along.
    if _status_rank(orphan["status"]) > _status_rank(survivor["status"]):
        updates["status"] = orphan["status"]

    # extra_data merge.
    merged_extra = _merge_extra_data(survivor["extra_data"], orphan["extra_data"])
    if merged_extra != survivor["extra_data"]:
        updates["extra_data"] = merged_extra

    # quantity: take max (sum would double-count on legit reprints, but
    # these pairs are same-print so max is the correct choice).
    if orphan["quantity"] is not None and (survivor["quantity"] is None or orphan["quantity"] > survivor["quantity"]):
        updates["quantity"] = orphan["quantity"]

    return updates


def _strict_candidates(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Race-dup candidates: same file_path + asymmetric library_file_id + <60s gap."""
    cur = con.cursor()
    cur.execute(
        """
        SELECT a.id AS a_id, b.id AS b_id, a.printer_id,
               (julianday(b.created_at) - julianday(a.created_at)) * 86400 AS gap_s
        FROM print_archives a
        JOIN print_archives b ON
            a.content_hash = b.content_hash
            AND a.printer_id = b.printer_id
            AND a.file_path = b.file_path
            AND a.id < b.id
            AND (julianday(b.created_at) - julianday(a.created_at)) * 86400 < 60
            AND ((a.library_file_id IS NOT NULL AND b.library_file_id IS NULL)
                 OR (a.library_file_id IS NULL AND b.library_file_id IS NOT NULL))
        ORDER BY a.id
        """
    )
    return cur.fetchall()


def _loose_candidates(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Broader candidates for manual review.

    Same content_hash + same printer + <5 minutes, where at least one side
    has ``status='archived'`` (characteristic of the orphan leg of the race).
    Excludes strict matches (printed separately).
    """
    cur = con.cursor()
    cur.execute(
        """
        SELECT a.id AS a_id, b.id AS b_id, a.printer_id,
               (julianday(b.created_at) - julianday(a.created_at)) * 86400 AS gap_s,
               a.status AS a_status, b.status AS b_status,
               a.library_file_id AS a_lib, b.library_file_id AS b_lib,
               a.file_path = b.file_path AS same_path
        FROM print_archives a
        JOIN print_archives b ON
            a.content_hash = b.content_hash
            AND a.printer_id = b.printer_id
            AND a.id < b.id
            AND (julianday(b.created_at) - julianday(a.created_at)) * 86400 < 300
            AND (a.status = 'archived' OR b.status = 'archived')
        ORDER BY a.id
        """
    )
    return cur.fetchall()


def _queue_referenced_ids(con: sqlite3.Connection) -> set[int]:
    cur = con.cursor()
    cur.execute("SELECT archive_id FROM print_queue WHERE archive_id IS NOT NULL")
    return {r[0] for r in cur.fetchall()}


def _fetch_row(con: sqlite3.Connection, archive_id: int) -> sqlite3.Row | None:
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM print_archives WHERE id = ?", (archive_id,))
    return cur.fetchone()


def _format_row_summary(r: sqlite3.Row) -> str:
    return (
        f"id={r['id']} status={r['status']} lib={r['library_file_id']} "
        f"proj={r['project_id']} by={r['created_by_id']} "
        f"started={r['started_at']} completed={r['completed_at']}"
    )


def merge_pair(
    con: sqlite3.Connection,
    a_id: int,
    b_id: int,
    queue_referenced_ids: set[int],
    *,
    apply: bool,
    log: list[str],
) -> None:
    a = _fetch_row(con, a_id)
    b = _fetch_row(con, b_id)
    if a is None or b is None:
        log.append(f"  skip: {a_id}/{b_id} not found (already merged?)")
        return

    survivor, orphan = _pick_survivor(a, b, queue_referenced_ids)
    updates = _merge_fields(survivor, orphan)

    log.append(f"  survivor: {_format_row_summary(survivor)}")
    log.append(f"  orphan:   {_format_row_summary(orphan)}")
    if updates:
        log.append(f"  will update survivor: {sorted(updates)}")
    else:
        log.append("  no field changes needed on survivor")

    if not apply:
        return

    # 1) Update survivor.
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [survivor["id"]]
        con.execute(f"UPDATE print_archives SET {set_clause} WHERE id = ?", params)

    # 2) Re-point any queue rows pointing at the orphan.
    cur = con.cursor()
    cur.execute(
        "UPDATE print_queue SET archive_id = ? WHERE archive_id = ?",
        (survivor["id"], orphan["id"]),
    )
    if cur.rowcount:
        log.append(f"  repointed {cur.rowcount} print_queue row(s) → {survivor['id']}")

    # 3) Delete orphan.
    con.execute("DELETE FROM print_archives WHERE id = ?", (orphan["id"],))
    log.append(f"  deleted orphan archive id={orphan['id']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("data/bamdude.db"))
    ap.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run).")
    ap.add_argument("--loose", action="store_true", help="Also print broader candidates for manual review.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"error: db not found at {args.db}", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    queue_ref = _queue_referenced_ids(con)

    strict = _strict_candidates(con)
    print(f"Strict race-dup pairs: {len(strict)}")
    log: list[str] = []
    for row in strict:
        log.append(f"\npair id={row['a_id']}+{row['b_id']} printer={row['printer_id']} gap={row['gap_s']:.1f}s")
        merge_pair(con, row["a_id"], row["b_id"], queue_ref, apply=args.apply, log=log)
    print("\n".join(log))

    if args.loose:
        loose = _loose_candidates(con)
        # Drop ones already covered by strict.
        strict_pairs = {(r["a_id"], r["b_id"]) for r in strict}
        loose = [r for r in loose if (r["a_id"], r["b_id"]) not in strict_pairs]
        print(f"\nLoose candidates (for manual review, NOT merged): {len(loose)}")
        for row in loose:
            print(
                f"  pair id={row['a_id']}+{row['b_id']} printer={row['printer_id']} "
                f"gap={row['gap_s']:.1f}s same_path={bool(row['same_path'])} "
                f"a=(status={row['a_status']} lib={row['a_lib']}) "
                f"b=(status={row['b_status']} lib={row['b_lib']})"
            )

    if args.apply:
        con.commit()
        print("\nCommitted.")
    else:
        con.rollback()
        print("\nDry-run — no changes written. Re-run with --apply to commit.")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
