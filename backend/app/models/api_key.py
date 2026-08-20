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
    # File-manager scope (upstream Bambuddy GHSA-r2qv-8222-hqg3 / v0.2.4.5).
    # Gates library upload / rename / delete-own + MakerWorld import. A distinct
    # trust level from queue management, hence its own flag rather than folding
    # into ``can_queue``. Default True mirrors ``can_queue`` so existing
    # "queue-only" keys keep their prior upload+queue workflow after upgrade;
    # the m086 backfill sets it to ``can_queue`` per-row so a hardened
    # read-only key (can_queue=False) does NOT silently gain writes.
    can_manage_library: Mapped[bool] = mapped_column(Boolean, default=True)
    # Inventory write scope (same PR). Covers spool/catalog/forecast writes and
    # the SpoolBuddy-kiosk write endpoints (NFC scan, scale reading) which used
    # INVENTORY_UPDATE as a stand-in under the prior denylist model. Read-only
    # inventory stays under ``can_read_status``. Default True mirrors
    # ``can_queue``; m086 backfills per-row.
    can_manage_inventory: Mapped[bool] = mapped_column(Boolean, default=True)
    # Maintenance write scope (upstream Bambuddy #1832 follow-up). Carves the
    # per-printer maintenance CRUD (log/reset items, edit intervals) + the
    # maintenance-type catalog CRUD out of the admin denylist so HA-style
    # automations can record "cleaned nozzle" via API key without broader
    # printer control. Default True mirrors the other manage_* scopes; the m104
    # backfill sets existing rows to False (these perms were previously
    # DENIED for every key, so no silent scope widening on upgrade).
    can_manage_maintenance: Mapped[bool] = mapped_column(Boolean, default=True)
    # Archive write scope (upstream Bambuddy #1888). Carves ARCHIVES_CREATE /
    # _UPDATE_* / _DELETE_* (NOT purge) out of the admin denylist so automations
    # can prune old prints via API key. OWN + ALL fold into this one scope (keys
    # have no per-row ownership identity). Default True; m104 backfills existing
    # rows to False.
    can_manage_archives: Mapped[bool] = mapped_column(Boolean, default=True)
    # Project write scope (upstream Bambuddy #1893). Carves PROJECTS_CREATE /
    # _UPDATE / _DELETE + membership (add-archives, gated on PROJECTS_UPDATE) out
    # of the admin denylist so automations can organize prints into projects.
    # Default True; m104 backfills existing rows to False.
    can_manage_projects: Mapped[bool] = mapped_column(Boolean, default=True)
    # Direct-to-device label printing: poll for work and queue a label.
    # ⚠️ Defaults **False**, unlike the can_manage_* columns above. Those split a
    # capability keys already had, so m104 backfilling silence to False would
    # have taken something away; this one is new, so nobody loses anything by
    # not being granted it. It is deliberately narrow — a bridge key sits on a
    # desktop somewhere and must not reach the library or the inventory.
    can_print_labels: Mapped[bool] = mapped_column(Boolean, default=False)

    # Optional scope limits
    printer_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # null = all printers

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # Optional expiry
