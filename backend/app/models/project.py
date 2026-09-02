"""Project = an ORDER: N units of one or more products for a customer.

Everything that described HOW to make the thing (targets, plan rows, part
targets, BOM, templates) moved to ``Product``; the hierarchy and the
``archived`` status are gone (spec 2026-09-02). ``status`` is active |
completed | cancelled and is closed by the operator, never automatically.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Hex colour for UI badges
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | completed | cancelled
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # HTML (TipTap)
    # Order-level documents: [{"filename", "original_name", "size", "uploaded_at"}]
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")  # low | normal | high | urgent
    # What the customer pays; margin = price - cost of the order's archives.
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)  # http(s) only (schema-validated)
    cover_image_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    customer: Mapped["Customer | None"] = relationship(back_populates="projects")
    lines: Mapped[list["ProjectLine"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectLine.sort_order"
    )
    procurement: Mapped[list["ProjectProcurement"]] = relationship(cascade="all, delete-orphan")
    archives: Mapped[list["PrintArchive"]] = relationship(back_populates="project")
    queue_items: Mapped[list["PrintQueueItem"]] = relationship(back_populates="project")


from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.customer import Customer  # noqa: E402
from backend.app.models.print_queue import PrintQueueItem  # noqa: E402
from backend.app.models.project_line import ProjectLine, ProjectProcurement  # noqa: E402
