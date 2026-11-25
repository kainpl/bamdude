"""Order lines and procurement facts (spec §Data model)."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class ProjectLine(Base):
    """``product × quantity`` inside an order. ``material`` is a HARD filter
    (filament type token, e.g. ``PETG``); ``color`` is a soft hint, displayed
    and never matched."""

    __tablename__ = "project_lines"
    __table_args__ = (
        CheckConstraint("from_finished >= 0", name="ck_project_lines_from_finished"),
        CheckConstraint("assembled >= 0", name="ck_project_lines_assembled"),
        CheckConstraint("received >= 0", name="ck_project_lines_received"),
        CheckConstraint("issued >= 0", name="ck_project_lines_issued"),
        CheckConstraint("returned >= 0", name="ck_project_lines_returned"),
        CheckConstraint("written_off >= 0", name="ck_project_lines_written_off"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # A deleted SQLite integer id may be reused. A retained plan must not
    # silently become intent for that new line, even with identical contents.
    plan_identity: Mapped[str | None] = mapped_column(String(36), nullable=True, default=lambda: str(uuid4()))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    # No ondelete: deleting a referenced product is refused with 409 in the route.
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    material: Mapped[str | None] = mapped_column(String(50), nullable=True)
    color: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # spec workshop-product-variants, rule 4: ``product`` (quantity × the
    # configured kit) or ``parts`` (a count of each part, quantity fixed at 1).
    # ``config_key`` is written only by services/line_config.py.
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="product", server_default="product")
    config_key: Mapped[str] = mapped_column(String(512), nullable=False, default="", server_default="")
    # Each entry is a complete, immutable revision. The last is the current kit.
    composition_revisions: Mapped[list[dict] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    # Snapshot on creation: editing catalog percentages never rewrites an existing order.
    extra_percentages: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    # Signed corrections to attributed printed output, not a replacement total.
    # Only order_recount writes; new prints keep adding normally. Copies start empty.
    part_count_adjustments: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    # spec workshop-add-to-order, rule 1: finished units this line took off the
    # finished-goods shelf, issued ones included (an issue does not lower it —
    # a completed order stays covered). Written only by services/finished_stock.py,
    # in the same flush as the movement that explains it.
    from_finished: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # spec workshop-order-issue, rule 3: what reached the shelf under the order and
    # what left it — assembled from the reserved kits, received from the prints,
    # issued to the customer, returned to free stock (cancel / delete). Only ever
    # grow; written only by services/finished_stock.py with the movement that
    # explains them. Held on the shelf = from_finished + assembled + received
    # − issued − returned − written_off (rule 7; spec workshop-order-issue-followups, rule 44).
    assembled: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    received: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    issued: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # spec workshop-order-issue-followups, rule 44: units held for the order and written off
    # (broken on the shelf) — the order needs them made again. Only grows.
    written_off: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship(back_populates="lines")


class ProjectLinePartStock(Base):
    """A parts line's part on the shelf under its order (spec workshop-order-issue,
    rule 4) — received, issued, returned and written off (spec workshop-order-issue-followups,
    rule 44); written only by ``services/part_stock.py``."""

    __tablename__ = "project_line_part_stock"
    __table_args__ = (
        CheckConstraint("received >= 0", name="ck_project_line_part_stock_received"),
        CheckConstraint("issued >= 0", name="ck_project_line_part_stock_issued"),
        CheckConstraint("returned >= 0", name="ck_project_line_part_stock_returned"),
        CheckConstraint("written_off >= 0", name="ck_project_line_part_stock_written_off"),
    )

    line_id: Mapped[int] = mapped_column(ForeignKey("project_lines.id", ondelete="CASCADE"), primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("product_parts.id", ondelete="CASCADE"), primary_key=True)
    received: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    issued: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    written_off: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ProjectProcurement(Base):
    """How many of a purchased part an order has acquired so far."""

    __tablename__ = "project_procurement"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    product_part_id: Mapped[int] = mapped_column(ForeignKey("product_parts.id", ondelete="CASCADE"), primary_key=True)
    quantity_acquired: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


from backend.app.models.product import Product  # noqa: E402
from backend.app.models.project import Project  # noqa: E402
