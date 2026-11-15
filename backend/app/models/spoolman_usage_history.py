from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SpoolmanUsageHistory(Base):
    """One print's landed charge to one Spoolman spool (m228, upstream 1d86cff5a).

    A table of its own, never a column on ``spool_usage_history``: that table is
    keyed by built-in spool ids, and a Spoolman id equal to one of them would
    borrow its prints (upstream 704919339). Nothing reads these rows in the
    internal inventory mode, and nothing reads the internal rows in Spoolman
    mode.

    ``source_token`` names the Spoolman this installation was talking to when
    the charge landed (``services/spoolman_usage_history``): a changed Spoolman
    address rotates it, and readers show only the current token's rows.
    ``spool_registered`` is Spoolman's own ``registered`` stamp of the spool at
    charge time — with the id it names one spool, so a later spool Spoolman
    gave the same id to is not this one. The SKU columns are a snapshot: the
    row stays readable after its spool is deleted in Spoolman.

    Written only by ``services/spoolman_usage_history`` (guard test).
    """

    __tablename__ = "spoolman_usage_history"
    __table_args__ = (
        Index("ix_spoolman_usage_history_source_created", "source_token", "created_at"),
        Index("ix_spoolman_usage_history_spool", "source_token", "spoolman_spool_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_token: Mapped[str] = mapped_column(String(32))
    spoolman_spool_id: Mapped[int] = mapped_column(Integer)
    spool_registered: Mapped[str | None] = mapped_column(String(40))
    printer_id: Mapped[int | None] = mapped_column(ForeignKey("printers.id", ondelete="SET NULL"))
    print_name: Mapped[str | None] = mapped_column(String(500))
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id"), nullable=True)
    weight_used: Mapped[float] = mapped_column(Float, default=0)
    percent_used: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="completed")
    cost: Mapped[float | None] = mapped_column(Float)
    # Stamped by the writer in Python UTC (naive), never a server default: that
    # is local time on a PostgreSQL whose timezone is not UTC.
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # The spool as it was when the charge landed (NULL when Spoolman did not
    # hand the spool over — the charge is history all the same).
    material: Mapped[str | None] = mapped_column(String(100))
    subtype: Mapped[str | None] = mapped_column(String(200))
    brand: Mapped[str | None] = mapped_column(String(200))
    color_name: Mapped[str | None] = mapped_column(String(200))
    rgba: Mapped[str | None] = mapped_column(String(8))
    slicer_filament_name: Mapped[str | None] = mapped_column(String(200))
    label_weight: Mapped[float | None] = mapped_column(Float)
