"""One run of an external-folder scan.

The scan used to be the request: it walked the share, wrote every row and only
then answered. On a NAS that took minutes, during which it held SQLite's write
lock — so anything else that tried to write failed with ``database is locked``,
and the traceback named whichever innocent query happened to be next.

A row here is what replaces that. The request creates it and returns; the worker
fills it in; the UI watches it over the socket.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class LibraryScanJob(Base):
    __tablename__ = "library_scan_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[int] = mapped_column(Integer, ForeignKey("library_folders.id", ondelete="CASCADE"), index=True)
    #: ``queued`` → ``running`` → ``finished`` | ``failed``.
    #:
    #: ⚠️ A row left ``running`` by a restart is swept to ``failed`` at startup.
    #: The process that would have finished it is gone, and ``running`` reads on
    #: screen as progress — the same trap the label queue had to grow a sweeper
    #: for.
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: How many files the walk turned up, which is what makes progress a
    #: fraction rather than a rising number with no end in sight.
    files_total: Mapped[int] = mapped_column(Integer, default=0)

    # ⚠️ Every counter defaults to 0, never NULL. A job that has done nothing has
    # done zero; NULL would render as "null files" and poison any sum.
    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    files_added: Mapped[int] = mapped_column(Integer, default=0)
    files_updated: Mapped[int] = mapped_column(Integer, default=0)
    files_removed: Mapped[int] = mapped_column(Integer, default=0)
    folders_added: Mapped[int] = mapped_column(Integer, default=0)
    folders_removed: Mapped[int] = mapped_column(Integer, default=0)

    #: ⚠️ True when the walk came back empty against a folder that has records,
    #: which is an unreachable mount rather than an emptied folder. Nothing was
    #: deleted, and the UI has to say why — silence here reads as "scan found no
    #: changes", and the next scan would do the same thing for the same reason.
    skipped_deletions: Mapped[bool] = mapped_column(Boolean, default=False)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # "Is a scan already running for this folder" is asked on every start.
        Index("ix_library_scan_jobs_folder_status", "folder_id", "status"),
    )
