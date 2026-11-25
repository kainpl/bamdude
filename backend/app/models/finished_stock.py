"""Finished goods — one stock position per product configuration (spec workshop-finished-goods, rules 1–5).

The balance is two columns the ONE writer (``services/finished_stock.py``)
keeps equal to the sum of its ledger; the ledger is the history. A position's
configuration is written by ``services/line_config.py`` — the same writer, and
the same rules, as an order line's — and read through
``services/line_composition.py``.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

#: The closed list of what may move a position (spec rule 4). WS-10/11 add their
#: order-bound kinds here.
MOVEMENT_KINDS = ("receipt", "stocktake", "assembled", "produced", "reserve", "release", "issue", "written_off")


def utcnow() -> datetime:
    """The ledgers' clock: naive UTC, stamped in Python (spec rule 13) — never a
    server default, which is local time on a PostgreSQL whose zone is not UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StockItem(Base):
    """A stock position: finished units of one product configuration."""

    __tablename__ = "stock_items"
    __table_args__ = (
        UniqueConstraint("product_id", "config_key", name="uq_stock_items_product_config"),
        CheckConstraint("on_hand >= 0", name="ck_stock_items_on_hand"),
        CheckConstraint("reserved >= 0", name="ck_stock_items_reserved"),
        CheckConstraint("reserved <= on_hand", name="ck_stock_items_reserved_le_on_hand"),
        # A new table whose id becomes a code: never hand an id out twice
        # (inv-workshop-codes-derived-from-id).
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    #: ``line_composition.config_key`` of the position's configuration — the
    #: same format as an order line's, so the two compare as strings.
    config_key: Mapped[str] = mapped_column(String(512), nullable=False, default="", server_default="")
    # The operator's variant groups the shelf. The captured kit distinguishes
    # physical units received before and after a change to the catalog.
    variant_key: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    composition_parts: Mapped[list[dict] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    location: Mapped[str | None] = mapped_column(String(64), nullable=True)
    min_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class StockItemChoice(Base):
    """The option a position has in one group — written for every group, like a line's."""

    __tablename__ = "stock_item_choices"

    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("product_variant_groups.id", ondelete="CASCADE"), primary_key=True)
    option_id: Mapped[int] = mapped_column(ForeignKey("product_variant_options.id"), index=True)


class StockItemPartCount(Base):
    """A per-unit count that differs from what the position's options give (0 = not in the kit)."""

    __tablename__ = "stock_item_part_counts"

    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id", ondelete="CASCADE"), primary_key=True)
    part_id: Mapped[int] = mapped_column(
        ForeignKey("product_parts.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)


class StockItemMovement(Base):
    """One movement of a position: what changed in each column, who, and why."""

    __tablename__ = "stock_item_movements"
    __table_args__ = (
        Index("ix_stock_item_movements_item_created", "item_id", "created_at"),
        Index("ix_stock_item_movements_created_id", "created_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    delta_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    delta_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: The operator's words; a movement the server writes on its own carries a token.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    # Order-bound movements arrive with WS-10/11; the columns are here so they only add doors.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    project_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_lines.id", ondelete="SET NULL"), nullable=True
    )
    #: The issue an ``issue`` movement belongs to (spec workshop-order-issue, rule 2).
    stock_issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_issues.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Who did it; NULL — the system.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
