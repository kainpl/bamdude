from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class APIKey(Base):
    """API key for external webhook access."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))  # User-friendly name
    key_hash: Mapped[str] = mapped_column(String(64))  # SHA256 hash of the key
    key_prefix: Mapped[str] = mapped_column(String(8))  # First 8 chars for identification

    # Owner — keys created via UI are stamped with the creating user's id so
    # cloud-aware routes can resolve "the key's user" and reuse that user's
    # per-user Bambu Cloud token. Nullable for legacy / programmatically
    # provisioned keys (#1182). ON DELETE CASCADE so deleting a user takes
    # their keys with them — SQLite needs an explicit DELETE in the route
    # because PRAGMA foreign_keys is off by default.
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Permissions
    can_queue: Mapped[bool] = mapped_column(Boolean, default=True)  # Add to queue
    can_control_printer: Mapped[bool] = mapped_column(Boolean, default=False)  # Start/stop/cancel
    can_read_status: Mapped[bool] = mapped_column(Boolean, default=True)  # Query status
    # Gates the key for cloud-token-backed endpoints (slice + slicer-presets,
    # #1182). Default False so legacy keys never silently spend the owner's
    # cloud token; flipping to True at create / update time is rejected for
    # ownerless keys (would have nothing to spend).
    can_access_cloud: Mapped[bool] = mapped_column(Boolean, default=False)
    # Narrowly-scoped opt-in for ``POST /settings/electricity-price`` so
    # Home-Assistant dynamic-tariff integrations can update
    # ``energy_cost_per_kwh`` via API key. Does NOT grant general
    # ``SETTINGS_UPDATE`` — full PATCH /settings remains denied for API
    # keys because it can rewrite SMTP / LDAP / MQTT credentials.
    # Default False so existing keys never silently gain settings-write
    # capability on upgrade. Upstream Bambuddy #1356 / commit ae29a7dc.
    can_update_energy_cost: Mapped[bool] = mapped_column(Boolean, default=False)

    # Optional scope limits
    printer_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # null = all printers

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # Optional expiry
