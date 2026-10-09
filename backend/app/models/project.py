"""Project = an ORDER: N units of one or more products for a customer.

Everything that described HOW to make the thing (targets, plan rows, part
targets, BOM, templates) moved to ``Product``; the hierarchy and the
``archived`` status are gone (spec 2026-09-02). ``status`` is active |
completed | cancelled and is closed by the operator, never automatically.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    auto_eject_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    auto_eject_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    # Who receives this order — a contact of ITS customer (checked in the route).
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    # Set by hand only (spec workshop-order-stage, rule 4): prep | printing | qc.
    # «Done» is status=completed and is never stored here.
    stage: Mapped[str] = mapped_column(String(16), default="prep", server_default="prep")
    # Any active user (rule 9); nulled in code when the user is deleted.
    responsible_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    customer: Mapped["Customer | None"] = relationship(back_populates="projects")
    contact: Mapped["CustomerContact | None"] = relationship()
    responsible: Mapped["User | None"] = relationship()
    lines: Mapped[list["ProjectLine"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectLine.sort_order"
    )
    procurement: Mapped[list["ProjectProcurement"]] = relationship(cascade="all, delete-orphan")
    archives: Mapped[list["PrintArchive"]] = relationship(back_populates="project")
    queue_items: Mapped[list["PrintQueueItem"]] = relationship(back_populates="project")


class ProjectEvent(Base):
    """One line of an order's journal (spec workshop-order-stage, part В).

    Written ONLY by ``services/order_journal.py`` — a test scans the tree for any
    other writer. ``payload`` carries codes, ids, numbers and name snapshots; the
    sentence is the frontend's, in the reader's language.
    """

    __tablename__ = "project_events"
    # A new table: never hand an id out twice (inv-workshop-codes-derived-from-id).
    __table_args__ = (
        Index("ix_project_events_project_created", "project_id", "created_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.customer import Customer, CustomerContact  # noqa: E402
from backend.app.models.print_queue import PrintQueueItem  # noqa: E402
from backend.app.models.project_line import ProjectLine, ProjectProcurement  # noqa: E402
from backend.app.models.user import User  # noqa: E402
