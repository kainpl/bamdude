"""Pure association tables for library files / folders ↔ projects M2M.

m044 introduced these pivots so a single library file or folder can live
in multiple projects simultaneously. They have no extra columns beyond
the two FKs that compose their primary key — pure many-to-many. Both
FKs cascade on delete so dropping a project, file, or folder cleans up
the corresponding pivot rows automatically (PostgreSQL enforces; SQLite
needs ``PRAGMA foreign_keys=ON``, which BamDude doesn't set globally,
so the ORM-level relationship cascades + explicit cleanup carry that
weight on SQLite).

These are exposed as ``Table`` objects (not ``Base`` subclasses) so the
M2M relationships in :mod:`backend.app.models.library` and
:mod:`backend.app.models.project` can reference them via the
``secondary=`` argument without an extra association class.
"""

from sqlalchemy import Column, ForeignKey, Integer, Table

from backend.app.core.database import Base

library_file_projects = Table(
    "library_file_projects",
    Base.metadata,
    Column(
        "file_id",
        Integer,
        ForeignKey("library_files.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "project_id",
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

library_folder_projects = Table(
    "library_folder_projects",
    Base.metadata,
    Column(
        "folder_id",
        Integer,
        ForeignKey("library_folders.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "project_id",
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)
