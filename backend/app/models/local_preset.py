"""Model for locally stored slicer presets (imported from OrcaSlicer, etc.)."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class LocalPreset(Base):
    """A locally stored slicer preset, typically imported from OrcaSlicer."""

    __tablename__ = "local_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    preset_type: Mapped[str] = mapped_column(String(20))  # filament, printer, process
    source: Mapped[str] = mapped_column(String(50), default="orcaslicer")  # orcaslicer, manual

    # Core fields extracted for filtering / AMS config
    filament_type: Mapped[str | None] = mapped_column(String(50))
    filament_vendor: Mapped[str | None] = mapped_column(String(200))
    nozzle_temp_min: Mapped[int | None] = mapped_column(Integer)
    nozzle_temp_max: Mapped[int | None] = mapped_column(Integer)
    pressure_advance: Mapped[str | None] = mapped_column(String(50))
    default_filament_colour: Mapped[str | None] = mapped_column(String(50))
    filament_cost: Mapped[str | None] = mapped_column(String(50))
    filament_density: Mapped[str | None] = mapped_column(String(50))
    compatible_printers: Mapped[str | None] = mapped_column(Text)  # JSON array

    # Full resolved JSON blob
    setting: Mapped[str] = mapped_column(Text)

    # Inheritance info
    inherits: Mapped[str | None] = mapped_column(String(300))
    version: Mapped[str | None] = mapped_column(String(50))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
