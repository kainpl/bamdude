"""Add ``virtual_printers.target_folder_id`` (Audit-2 0.4.2).

Per-VP destination folder in the library. When set, files arriving at this
VP land directly into that folder; NULL = library root. Used by the new
VP redesign that saves incoming 3MFs to the library + queues them with
``library_file_id`` instead of pre-creating a ``status='archived'``
archive placeholder row (see ``temp/dead-code-audit-0.4.2.md`` Audit-2).

``ON DELETE SET NULL`` so deleting the folder leaves the VP working
(falls back to root) instead of cascading and breaking the VP config.
"""

from backend.app.migrations.helpers import add_column

version = 40
name = "vp_target_folder"


async def upgrade(conn):
    # SQLite doesn't enforce ON DELETE actions without PRAGMA foreign_keys=ON
    # (which BamDude doesn't set globally), so the FK clause is documentation
    # for Postgres + future-proofing if we ever turn FK enforcement on.
    await add_column(
        conn,
        "virtual_printers",
        "target_folder_id INTEGER REFERENCES library_folders(id) ON DELETE SET NULL",
    )


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
