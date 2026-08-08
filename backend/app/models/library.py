"""Library models for file manager functionality."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, Select, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class LibraryFolder(Base):
    """Folder for organizing library files."""

    __tablename__ = "library_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("library_folders.id", ondelete="CASCADE"), nullable=True)

    # External folder flags (for folders that point to external paths)
    is_external: Mapped[bool] = mapped_column(Boolean, default=False)
    external_readonly: Mapped[bool] = mapped_column(Boolean, default=False)
    external_show_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    external_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Link to archive (optional). Project links live in the
    # ``library_folder_projects`` pivot — see ``projects`` below.
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="SET NULL"), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Real on-disk mtime for external (mapped/NAS) folders, refreshed on every
    # scan. Nullable on purpose: internal folders have no disk directory of
    # their own, and external rows scanned before this column existed backfill
    # on the next scan. Readers must COALESCE onto ``updated_at`` so both cases
    # still contribute a sort key (#2680).
    fs_modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    parent: Mapped["LibraryFolder | None"] = relationship(
        "LibraryFolder",
        back_populates="children",
        remote_side="LibraryFolder.id",
        foreign_keys="LibraryFolder.parent_id",
    )
    children: Mapped[list["LibraryFolder"]] = relationship(
        "LibraryFolder",
        back_populates="parent",
        foreign_keys="LibraryFolder.parent_id",
        cascade="all, delete-orphan",
    )
    files: Mapped[list["LibraryFile"]] = relationship(
        back_populates="folder",
        cascade="all, delete-orphan",
    )
    # m044: many-to-many. A folder can belong to any number of projects.
    projects: Mapped[list["Project"]] = relationship(
        "Project",
        secondary="library_folder_projects",
        back_populates="library_folders",
    )
    archive: Mapped["PrintArchive | None"] = relationship()


class LibraryFile(Base):
    """File stored in the library."""

    __tablename__ = "library_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("library_folders.id", ondelete="CASCADE"), nullable=True)
    # m044: project links live in ``library_file_projects`` pivot — see
    # ``projects`` relationship below. The legacy ``project_id`` column
    # is gone after the migration runs.

    # External file flag
    is_external: Mapped[bool] = mapped_column(Boolean, default=False)

    # File info
    filename: Mapped[str] = mapped_column(String(255))  # Original filename
    file_path: Mapped[str] = mapped_column(String(500))  # Storage path
    # Primary format identity — singular value: "3mf"/"gcode"/"stl"/"step"/
    # "stp"/"unknown". Sliced 3MFs (container ".gcode.3mf") collapse to
    # "gcode" so the file-manager surfaces them under the same affordances
    # as raw .gcode files. Composite identity (e.g. "sliced 3MF carries
    # both gcode and 3mf badges") lives in ``file_tags`` below — this
    # column stays single-string so legacy filters / FTS / Telegram
    # listings keep working unchanged.
    file_type: Mapped[str] = mapped_column(String(10))
    # Composite tag array driving frontend badges + chip-row multi-select
    # filter. Computed by :func:`services.library_helpers.compute_file_tags`
    # at every write site; the m036 migration backfills existing rows from
    # ``file_type`` + ``file_metadata`` + ``source_type`` + ``swap_compatible``.
    # Tags currently emitted: format (3mf/gcode/stl/step), structure
    # (multiplate/swap), provenance (sliced/makerworld/project).
    file_tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    file_size: Mapped[int] = mapped_column(Integer)
    file_hash: Mapped[str | None] = mapped_column(String(64))  # SHA256 for duplicate detection
    thumbnail_path: Mapped[str | None] = mapped_column(String(500))

    # Extracted metadata (from 3MF parser)
    file_metadata: Mapped[dict | None] = mapped_column(JSON)

    # Usage tracking. Increment on successful queue completion only
    # (failed/cancelled don't count) so the field reflects successful
    # usage rather than attempt count (#1008).
    print_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_printed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # User notes
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance — populated by MakerWorld import + slicer output (m033). NULL
    # for plain uploads. ``source_url`` is indexed because MakerWorld dedup
    # queries by canonical URL on every import.
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    # User tracking (Issue #206)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Swap mode compatibility (processed for plate swapper)
    swap_compatible: Mapped[bool] = mapped_column(Boolean, default=False)

    # Whether per-object skipping will work for this file — ``gcode_label_objects``
    # AND ``exclude_object``, both from ``Metadata/project_settings.config``.
    # Denormalised out of ``file_metadata`` (m114) so the file list can badge it
    # without a JSON dig per row, and so it stays filterable server-side. Always
    # written through ``services.library_helpers.skip_objects_supported_from_metadata``
    # — never inline — so the badge cannot disagree with the live 3MF gate the
    # preview reads.
    skip_objects_supported: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Real on-disk mtime, recorded by the external scan and refreshed on every
    # re-scan. This is the whole point of #2680: a bulk external scan stamps the
    # *same* ``updated_at`` on every row it touches, so sorting by it ties across
    # the whole batch and orders arbitrarily — nothing captured when the file was
    # actually written. Nullable so internal uploads and pre-column rows keep
    # working; readers COALESCE onto ``updated_at``.
    fs_modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Soft-delete / trash bin (#1008). When non-null, the file is in the
    # trash and should not appear in normal listings. A background sweeper
    # hard-deletes rows whose deleted_at is older than the retention window.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    @classmethod
    def active(cls) -> "Select[tuple[LibraryFile]]":
        """Select statement that excludes trashed (soft-deleted) files.

        Use this as the starting point for any user-facing library listing
        or lookup so trashed files don't leak into normal flows. Endpoints
        that specifically operate on trashed rows (trash list, restore,
        sweeper) build their own query with ``deleted_at.isnot(None)``.
        """
        return select(cls).where(cls.deleted_at.is_(None))

    # Relationships
    folder: Mapped["LibraryFolder | None"] = relationship(back_populates="files")
    # m044: many-to-many. A file can belong to any number of projects.
    projects: Mapped[list["Project"]] = relationship(
        "Project",
        secondary="library_file_projects",
        back_populates="library_files",
    )
    created_by: Mapped["User | None"] = relationship()
    # #1268 — user-authored cross-cutting tags (M2M). DISTINCT from the
    # ``file_tags`` JSON column above (m036 system-computed badges): this
    # relationship is the user-editable label catalog. Never conflate them.
    tags: Mapped[list["LibraryTag"]] = relationship(
        secondary="library_file_tags",
        back_populates="files",
    )
    # gh#3 - delete notes alongside the file at the ORM level; the DB FK also
    # has ON DELETE CASCADE for direct SQL deletes, but ORM cascade covers
    # in-session `db.delete(file)` calls regardless of SQLite pragma state.
    file_notes: Mapped[list["LibraryFileNote"]] = relationship(
        "LibraryFileNote",
        cascade="all, delete-orphan",
        # passive_deletes=False → ORM explicitly deletes children before the
        # parent, which works regardless of SQLite `PRAGMA foreign_keys` state.
        # PostgreSQL FKs handle it at the DB level too; both paths converge.
        passive_deletes=False,
    )
    # m056 — 1:1 MakerWorld source metadata. Same cascade rationale as
    # ``file_notes``: ORM-side deletion covers in-session deletes
    # regardless of SQLite FK pragma state; the DB FK also has CASCADE.
    makerworld_meta: Mapped["LibraryFileMakerworldMeta | None"] = relationship(
        "LibraryFileMakerworldMeta",
        back_populates="library_file",
        cascade="all, delete-orphan",
        passive_deletes=False,
        uselist=False,
    )


class LibraryTag(Base):
    """User-authored cross-cutting label for library files (#1268). name_key =
    LOWER(TRIM(name)) so case/space variants collide on the UNIQUE index → 409.
    NOT the same as LibraryFile.file_tags (computed system badges, m036)."""

    __tablename__ = "library_tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Uniqueness is COMPOSITE with ``is_system`` — see __table_args__. A plain
    # unique name_key would let a pre-existing user tag named after a system one
    # block the m128 seed, leaving only bad options: rename somebody's data, or
    # prefix every system row forever.
    name_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # System tags are handed out by the system: computed from the file, never
    # editable, never detachable. ``code`` is the stable identifier
    # (``3mf``, ``sliced``, …) that ``compute_file_tags`` emits; NULL on user
    # tags. The DISPLAY name stays in i18n — storing the rendered string here
    # would make system tags untranslatable — so ``name`` holds the English
    # label as a fallback for non-UI consumers (API clients, the Telegram bot).
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    files: Mapped[list["LibraryFile"]] = relationship(secondary="library_file_tags", back_populates="tags")

    __table_args__ = (
        # Indexes rather than UniqueConstraints: an index can be added to an
        # existing SQLite table with plain DDL, a table constraint cannot.
        Index("ix_library_tags_name_key_is_system", "name_key", "is_system", unique=True),
        # Nullable is fine — SQLite and PostgreSQL both treat NULLs as distinct
        # in a unique index, so every user tag carries NULL while no two system
        # rows can share a code.
        Index("ix_library_tags_code", "code", unique=True),
    )


class LibraryFileTag(Base):
    """Association between library files and user tags (#1268). Composite PK
    prevents dup (file,tag); both FKs ON DELETE CASCADE."""

    __tablename__ = "library_file_tags"

    file_id: Mapped[int] = mapped_column(ForeignKey("library_files.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("library_tags.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


from backend.app.models.archive import PrintArchive  # noqa: E402, F811
from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta  # noqa: E402, F401
from backend.app.models.library_file_note import LibraryFileNote  # noqa: E402, F401
from backend.app.models.project import Project  # noqa: E402, F811
from backend.app.models.user import User  # noqa: E402, F811
