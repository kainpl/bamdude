"""Thin link table between a Spoolman spool and a printer's K-profile cache row.

After m064 this is a pure link, mirroring :class:`SpoolKProfile` but FK-keyed
on a Spoolman spool ID rather than a local BamDude spool row, so the link
travels with the spool across BamDude installs pointing at the same Spoolman
backend. Actual K data lives on :class:`FilamentCalibration`.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class SpoolmanKProfile(Base):
    __tablename__ = "spoolman_k_profile"

    __table_args__ = (
        UniqueConstraint("spoolman_spool_id", "printer_id", "extruder", "filament_calibration_id"),
        CheckConstraint("extruder >= 0 AND extruder <= 1", name="ck_spoolman_kp_extruder_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    spoolman_spool_id: Mapped[int] = mapped_column(Integer, nullable=False)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    extruder: Mapped[int] = mapped_column(Integer, default=0)
    filament_calibration_id: Mapped[int] = mapped_column(
        ForeignKey("filament_calibration.id", ondelete="CASCADE"), nullable=False
    )
    # True when created by the auto-link engine (mirror of SpoolKProfile).
    auto_linked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    printer: Mapped["Printer"] = relationship()  # noqa: F821
    filament_calibration: Mapped["FilamentCalibration"] = relationship(lazy="selectin")


from backend.app.models.filament_calibration import FilamentCalibration  # noqa: E402, F401
from backend.app.models.printer import Printer  # noqa: E402, F401
