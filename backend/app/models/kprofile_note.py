"""Note attached to a calibration entry (per-row, stable identity).

Re-keyed in m065: was ``(printer_id, setting_id)`` — both unstable; now keyed
on ``filament_calibration_id`` which is our own stable PK. Notes survive
printer restarts, re-syncs and reorders.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class KProfileNote(Base):
    __tablename__ = "kprofile_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    filament_calibration_id: Mapped[int] = mapped_column(
        ForeignKey("filament_calibration.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    filament_calibration: Mapped["FilamentCalibration"] = relationship(lazy="selectin")


from backend.app.models.filament_calibration import FilamentCalibration  # noqa: E402, F401
