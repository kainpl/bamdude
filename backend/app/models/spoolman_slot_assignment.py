"""Spoolman spool ↔ AMS slot binding.

When BamDude is configured against a Spoolman backend, this is the source of
truth for which Spoolman spool occupies a given (printer, ams, tray) slot.
Spoolman's own ``spool.location`` field is left untouched — operators may
populate it manually for human-readable storage information.

Mirrors :class:`SpoolAssignment` (local-DB inventory) — both tables coexist;
the inventory route picks one based on ``settings.inventory_backend``.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class SpoolmanSlotAssignment(Base):
    __tablename__ = "spoolman_slot_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    ams_id: Mapped[int] = mapped_column(Integer)
    tray_id: Mapped[int] = mapped_column(Integer)
    spoolman_spool_id: Mapped[int] = mapped_column(Integer)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    printer: Mapped["Printer"] = relationship()  # noqa: F821

    __table_args__ = (
        UniqueConstraint("printer_id", "ams_id", "tray_id", name="uq_spoolman_slot_assignment"),
        # 0-7: standard AMS units. 128-191: AMS-HT (H2C / H2D — each AMS-HT
        # unit uses its own ams_id in that range, single tray per unit).
        # 255: external / virtual tray. Matches the value range the internal
        # ``spool_assignment`` table already accepts. Widened in m074 to fix
        # upstream Bambuddy #1274 — AMS-HT slot links were dying with
        # ``CHECK constraint failed: ck_spoolman_slot_ams_id_range``.
        CheckConstraint(
            "(ams_id >= 0 AND ams_id <= 7) OR (ams_id >= 128 AND ams_id <= 191) OR ams_id = 255",
            name="ck_spoolman_slot_ams_id_range",
        ),
        CheckConstraint("tray_id >= 0 AND tray_id <= 3", name="ck_spoolman_slot_tray_id_range"),
    )


from backend.app.models.printer import Printer  # noqa: E402, F401
