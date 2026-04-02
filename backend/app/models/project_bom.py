from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class ProjectBOMItem(Base):
    """Bill of Materials item for a project.

    Tracks sourced/purchased parts (hardware, electronics, screws, etc.)
    that need to be acquired for a project.
    """

    __tablename__ = "project_bom_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    quantity_needed: Mapped[int] = mapped_column(Integer, default=1)
    quantity_acquired: Mapped[int] = mapped_column(Integer, default=0)

    # Sourcing information
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sourcing_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Optional link to archive (for reference)
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="SET NULL"), nullable=True)

    # Reference to attachment filename
    stl_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Remarks about this part
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Sort order
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="bom_items")
    archive: Mapped["PrintArchive | None"] = relationship()


from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.project import Project  # noqa: E402
