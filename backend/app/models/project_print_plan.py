"""Per-project print plan: library files selected for printing with copies and order."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class ProjectPrintPlanItem(Base):
    """One row per (project, library_file) pair.

    Since m044 a library file can belong to multiple projects, so this
    table now carries one row per (project, file) combination — a file
    in N projects appears in N plans with independent ``copies`` /
    ``order_index``. The unique constraint pivots accordingly.

    Totals (grams, time, objects, cost) are computed on-the-fly from the
    joined ``LibraryFile.file_metadata`` × ``copies`` rather than cached
    here. Reslicing a 3MF automatically flows through without syncing.
    """

    __tablename__ = "project_print_plan_items"
    __table_args__ = (UniqueConstraint("project_id", "library_file_id", name="uq_plan_project_file"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    library_file_id: Mapped[int] = mapped_column(ForeignKey("library_files.id", ondelete="CASCADE"))

    copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship()
    library_file: Mapped["LibraryFile"] = relationship()


from backend.app.models.library import LibraryFile  # noqa: E402
from backend.app.models.project import Project  # noqa: E402
