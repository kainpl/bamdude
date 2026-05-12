"""ORM for filament_calibration (m062).

Per-filament-type cali storage. Many rows per combo (history), one
is_active=True per combo (enforced by partial unique index). Written by
CalibrationService.save_result after wizard completes; consumed by
background_dispatch.apply_active_calibration to fire extrusion_cali_sel
on the printer before each print start.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class FilamentCalibration(Base):
    __tablename__ = "filament_calibration"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Identity (combo)
    printer_model: Mapped[str] = mapped_column(String(50), nullable=False)
    filament_id: Mapped[str] = mapped_column(String(50), nullable=False)
    filament_setting_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nozzle_diameter: Mapped[float] = mapped_column(Float, nullable=False)
    nozzle_volume_type: Mapped[str] = mapped_column(String(20), nullable=False)
    extruder_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Result payload
    pa_k_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pa_n_coef: Mapped[float | None] = mapped_column(Float, nullable=True)
    flow_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Provenance
    cali_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cali_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    calibrated_on_printer_id: Mapped[int | None] = mapped_column(
        ForeignKey("printers.id", ondelete="SET NULL"), nullable=True
    )
    calibrated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_filament_cali_lookup",
            "printer_model",
            "filament_id",
            "nozzle_diameter",
            "nozzle_volume_type",
            "extruder_id",
        ),
    )
