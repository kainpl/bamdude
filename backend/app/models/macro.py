"""Macro model - reusable G-code snippets triggered by events."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class Macro(Base):
    """G-code macro triggered by specific events (e.g. swap mode start, table change)."""

    __tablename__ = "macros"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Display name
    name: Mapped[str] = mapped_column(String(100))

    # Free-form description / note shown in the editor. Useful for tracking
    # upstream version tags (e.g. ``swap-sequence_v05_20260312``), author
    # attribution, or usage caveats.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Target printer models - JSON array of model codes, e.g. '["A1 Mini"]' or '["*"]' for all
    printer_models: Mapped[str] = mapped_column(Text, default='["*"]')

    # Requires swap mode on the printer
    swap_mode_only: Mapped[bool] = mapped_column(Boolean, default=False)

    # Optional swap-profile binding (catalog key from core/swap_profiles.py).
    # Null = macro is not tied to any specific swap variant (generic fallback).
    # Set = macro only matches a printer whose ``swap_profile`` equals this value,
    # allowing multiple swap-mode gcode sets to coexist per model
    # (e.g. "a1mini_v1" vs "a1mini_v2" for different mechanical revisions).
    swap_profile: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Event/action trigger
    # swap_mode_start - injected before first print in swap sequence
    # swap_mode_change_table - injected between plates (table swap)
    # print_started - fires on gcode_state PREPARE→RUNNING transition
    event: Mapped[str] = mapped_column(String(50))

    # What kind of action this macro performs:
    # - ``gcode`` (default, legacy): send the ``gcode`` field as printer gcode
    # - ``mqtt_action``: invoke a named MQTT-level printer command
    #   (``chamber_light_off`` / ``chamber_light_on`` on MVP). ``gcode`` field
    #   is ignored for this type; the command code lives in ``mqtt_action``.
    action_type: Mapped[str] = mapped_column(String(20), default="gcode", server_default="gcode")

    # Named command from the MQTT-action catalog (core/mqtt_macro_actions.py).
    # Only meaningful when ``action_type='mqtt_action'``. Null for gcode macros.
    mqtt_action: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Optional delay before firing the action, in seconds. 0 = fire immediately
    # on the event. Useful for e.g. "turn light off 30s after print_started"
    # to let heat-up phase finish first.
    delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # G-code content
    gcode: Mapped[str] = mapped_column(Text, default="")

    # Custom macros can be deleted; built-in cannot
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)

    # Enabled/disabled
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
