"""ORM for calibration_session (m062).

Orchestration row for the wizard — NOT persistent storage of cali values.
Tracks: which mode + method, which printer, which user, current status,
linked print job (manual path), Flow Rate 2-stage chain via parent_session_id.

Lifecycle:
    running → awaiting_user_input → saved
            → cancelled
            → failed
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class CalibrationSession(Base):
    __tablename__ = "calibration_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    cali_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    nozzle_diameter: Mapped[float] = mapped_column(Float, nullable=False)
    nozzle_volume_type: Mapped[str] = mapped_column(String(20), nullable=False)
    extruder_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filaments_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    mqtt_sequence_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    print_queue_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_queue.id", ondelete="SET NULL"), nullable=True
    )

    parent_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_session.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    coarse_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    # JSON snapshot of the wizard args (preset refs, bundle, bed_type,
    # slicer, spec, print_options, swap_macros) captured at session
    # creation. Read by Flow Rate's `_start_flow_rate_stage2` so the
    # pass-2 dispatch can re-slice through the same sidecar + presets
    # the operator picked at the start of pass 1, without the wizard
    # having to round-trip the args back through the API. NULL for
    # AUTO sessions (no slicer dispatch — MQTT flow_rate_cali_start
    # path) and for any session created before m070 (no behaviour
    # change for already-running calibrations).
    dispatch_args_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_calibration_session_printer", "printer_id", "status", "created_at"),)
