"""Free stock of a product's printed parts — a LEDGER, never a counter.

One row per movement (``+`` in, ``−`` out); the balance of a part is
``SUM(delta)`` over its rows. The spec (pass 8, Decision 1) chose this shape
over a ``stock`` column because a ledger is auditable and reversible and a
counter is neither: every accounting bug this project has had was a counter
that drifted, and the only way to find out what it should have been was to
replay the events it had already thrown away.

⚠️ **``services/part_stock.py`` is the only writer of this table.** Every
reason a movement can carry, the refusal of a negative balance and the clamp
that keeps a stale dialog from over-reserving live there, in one place — a
second writer would have to re-decide all three, and would get one of them
wrong. Read through :func:`~backend.app.services.part_stock.balances` too;
that reader is the one that knows which parts count.

``Product.parts[].stock_balance`` is NOT a column here or on ``ProductPart``:
it is a sum, and a sum kept in a column is the counter this table exists to
replace. The API computes it on read.

The FK cascades fire on PostgreSQL only — this codebase never sets
``PRAGMA foreign_keys = ON`` — so a SQLite path that deletes a part, an order
line or an archive must clean up after itself exactly as the other pass-1
tables require.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

#: Why a movement happened. The set is closed: a reason outside it is a caller
#: bug, and :func:`~backend.app.services.part_stock.move` raises rather than
#: writing a row nothing downstream can read.
REASON_SURPLUS_BANKED = "surplus_banked"
REASON_UNFILED_PRINT = "unfiled_print"
REASON_RESERVED_FOR_ORDER = "reserved_for_order"
REASON_RESERVATION_RELEASED = "reservation_released"
REASON_MANUAL = "manual"


class ProductPartStockMovement(Base):
    __tablename__ = "product_part_stock_movements"
    # The only index the table needs: every read is "this part, newest first"
    # (a balance sums the rows, the product page lists them). It covers a
    # lookup by ``product_part_id`` alone as its own prefix, so the column
    # carries no second single-column index of its own.
    __table_args__ = (Index("ix_product_part_stock_movements_part_created", "product_part_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_part_id: Mapped[int] = mapped_column(ForeignKey("product_parts.id", ondelete="CASCADE"))
    #: Signed: ``+`` banks or releases into stock, ``−`` takes out of it.
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The order line a reservation belongs to (Decision 4) — also what
    #: "already banked for this line" is counted by (Decision 2).
    project_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_lines.id", ondelete="SET NULL"), nullable=True
    )
    #: The print this came from (Decision 3). Also the idempotency key for the
    #: order-less completion path: a second event for the same archive writes
    #: nothing because the ledger already names it.
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: NULL for the completion handler, which writes with no user (Decision 7).
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
