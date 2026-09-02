"""Order lines and procurement facts (spec §Data model)."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class ProjectLine(Base):
    """``product × quantity`` inside an order. ``material`` is a HARD filter
    (filament type token, e.g. ``PETG``); ``color`` is a soft hint, displayed
    and never matched."""

    __tablename__ = "project_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    # No ondelete: deleting a referenced product is refused with 409 in the route.
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    material: Mapped[str | None] = mapped_column(String(50), nullable=True)
    color: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship(back_populates="lines")


class ProjectProcurement(Base):
    """How many of a purchased part an order has acquired so far."""

    __tablename__ = "project_procurement"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    product_part_id: Mapped[int] = mapped_column(ForeignKey("product_parts.id", ondelete="CASCADE"), primary_key=True)
    quantity_acquired: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


from backend.app.models.product import Product  # noqa: E402
from backend.app.models.project import Project  # noqa: E402
