r"""Library, project and product attachments leave ``archive/`` for their own
DATA_DIR roots.

Vault 60-specs/data-dir-roots-spec. ``archive/`` is the print history - one
folder per run plus the 3MF download staging - and nothing else. The file
manager, the MakerWorld covers and the two attachment trees lived under it
because that is where the fork found them; the docs and the backup map already
named ``library/`` and ``projects/`` at the root, so this makes the code agree.

Two phases, in this order:

1. **Filesystem, before any DB write, idempotent.** ``archive/library`` ->
   ``library``, ``archive/projects`` -> ``projects``, ``archive/products`` ->
   ``products``. Every root is under DATA_DIR (``archive_dir`` is not
   redirectable), so a move is one ``os.rename`` of the directory; with a
   target already present the top-level entries are merged one rename each. An
   entry present on both sides, a target that is not a directory, or a
   cross-device rename stops startup with the path in the message - nothing is
   copied, nothing is deleted, the operator resolves it and starts again.
2. **Database, in the migration's transaction.** ``library_files.file_path`` /
   ``thumbnail_path`` and ``library_file_makerworld_meta.cover_path`` /
   ``variant_cover_path`` lose the ``archive`` segment when they start with
   ``archive/library/`` or ``archive\library\`` - separator kept, absolute
   (external-folder) paths untouched. Attachments are named by filename in
   ``projects.attachments`` / ``products.attachments``, so there is nothing to
   rewrite for them.

A crash between the phases leaves files moved and rows old; the next start
skips the missing sources and rewrites the rows. ``DEBUG=true`` re-runs the
latest migration on every start, so both phases must be no-ops the second time
- they are. A backup from before this change restores the old layout together
with an older ``_migrations``, so this runs again after that restore too.
"""

import errno
import logging
import os
from pathlib import Path

from sqlalchemy import text

from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 177
name = "data_dir_roots"

#: ``archive/<name>`` -> ``<name>``, in this order.
MOVES = ("library", "projects", "products")
#: Columns that name files under the old ``archive/library`` root.
REWRITES = (
    ("library_files", ("file_path", "thumbnail_path")),
    ("library_file_makerworld_meta", ("cover_path", "variant_cover_path")),
)
OLD_PREFIXES = ("archive/library/", "archive\\library\\")


class RootMoveConflict(RuntimeError):
    """A move the migration cannot decide about. Startup stops; the message names the paths."""


def _rename(src: Path, dst: Path) -> None:
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise RootMoveConflict(
                f"{src} and {dst} are on different filesystems; move the folder by hand, then start again"
            ) from e
        raise


def move_root(data_dir: Path, root: str) -> int:
    """``<data_dir>/archive/<root>`` -> ``<data_dir>/<root>``. Returns how many renames happened."""
    src, dst = data_dir / "archive" / root, data_dir / root
    if not src.is_dir():
        return 0
    if not dst.exists():
        _rename(src, dst)
        return 1
    if not dst.is_dir():
        raise RootMoveConflict(f"{dst} exists and is not a directory; move it aside, then start again")
    moved = 0
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        if target.exists():
            raise RootMoveConflict(f"{entry} cannot move: {target} already exists; keep one of them, then start again")
        _rename(entry, target)
        moved += 1
    src.rmdir()
    return moved


def rewrite(value):
    """The library path without its ``archive`` segment; anything else unchanged."""
    if not isinstance(value, str):
        return value
    for prefix in OLD_PREFIXES:
        if value.startswith(prefix):
            return value[len("archive") + 1 :]
    return value


async def upgrade(conn):
    from backend.app.core.config import settings

    data_dir = Path(settings.data_dir)
    for root in MOVES:
        moved = move_root(data_dir, root)
        if moved:
            logger.info("m177: archive/%s -> %s (%d rename(s))", root, root, moved)

    for table, columns in REWRITES:
        if not await table_exists(conn, table):
            continue
        cols = ", ".join(columns)
        rows = (await conn.execute(text(f"SELECT id, {cols} FROM {table}"))).mappings().all()  # noqa: S608
        changed = 0
        for row in rows:
            new = {c: rewrite(row[c]) for c in columns}
            if all(new[c] == row[c] for c in columns):
                continue
            sets = ", ".join(f"{c} = :{c}" for c in columns)
            await conn.execute(text(f"UPDATE {table} SET {sets} WHERE id = :id"), {"id": row["id"], **new})  # noqa: S608
            changed += 1
        if changed:
            logger.info("m177: %s: %d row(s) now name library/ at the root", table, changed)
