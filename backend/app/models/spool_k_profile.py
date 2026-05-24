"""Thin link table between a local spool and a printer's K-profile cache row.

After m064 this is a pure link: one row per ``(spool_id, printer_id, extruder)``
combo, with the actual K data (k_value / name / cali_idx / setting_id) living
on :class:`FilamentCalibration`. Many spools sharing the same printer-side
profile collapse to one ``filament_calibration`` row plus N links.

Multiple rows per (spool, printer, extruder) are allowed when the user has
calibrations for different nozzles on the same printer/extruder pair — they
are disambiguated by joining through ``filament_calibration.nozzle_diameter``.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class SpoolKProfile(Base):
    __tablename__ = "spool_k_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    spool_id: Mapped[int] = mapped_column(ForeignKey("spool.id", ondelete="CASCADE"))
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    extruder: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1 (H2D)
    filament_calibration_id: Mapped[int] = mapped_column(
        ForeignKey("filament_calibration.id", ondelete="CASCADE"), nullable=False
    )
    # True when this link was created by the auto-link engine
    # (services/kprofile_autolink.py). Manual links from the PA tab are
    # False and the engine never touches them ("B-simple" rule).
    auto_linked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    spool: Mapped["Spool"] = relationship(back_populates="k_profiles")
    printer: Mapped["Printer"] = relationship()
    # ``lazy="selectin"`` — auto-eager-load the joined cache row so existing
    # ``selectinload(Spool.k_profiles)`` queries return enriched rows without
    # a chained selectinload at every call site. (Pydantic's
    # ``SpoolKProfileResponse._enrich_from_link`` validator reads this.)
    filament_calibration: Mapped["FilamentCalibration"] = relationship(lazy="selectin")


from backend.app.models.filament_calibration import FilamentCalibration  # noqa: E402, F401
from backend.app.models.printer import Printer  # noqa: E402, F401
from backend.app.models.spool import Spool  # noqa: E402, F401
