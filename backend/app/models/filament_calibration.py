"""ORM for filament_calibration (m062 + m063).

Per-printer cache of K-profile data. The printer's 16-slot extrusion_cali
table is the source of truth; this is a stable-identity mirror so notes
and spool linkage (m064) have a fixed PK to attach to, surviving printer
reorders of cali_idx.

Many rows per combo (history); one is_active=True per combo (partial
unique index). Written by CalibrationService.save_result after the wizard
completes, AND by sync_printer_kprofiles_to_cache whenever BamDude reads
the printer's live list (route handler, save_result round-trip, apply-path
cache miss). Consumed by background_dispatch's pre-print hook +
apply_active_calibration_to_slot helper, which resolve LIVE cali_idx by
matching stable identity in client.state.kprofiles before firing
extrusion_cali_sel.

m063: scope changed from printer_model (cross-instance share) to
printer_id (per-instance precision). Two X1Cs in a farm can need
different K values for the same material.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class FilamentCalibration(Base):
    __tablename__ = "filament_calibration"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Identity (combo) — per m063 keyed by printer_id, not printer_model
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
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
    # Volatile printer-side index — refreshed by sync, never used as identity.
    cali_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance for "who/where calibrated" — distinct from printer_id which is
    # the scope. Useful when the calibration was first introduced on a different
    # printer of the same model and copied here.
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
            "printer_id",
            "filament_id",
            "nozzle_diameter",
            "nozzle_volume_type",
            "extruder_id",
        ),
    )
