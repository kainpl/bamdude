from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

if TYPE_CHECKING:
    from backend.app.models.printer_location import PrinterLocation


class Printer(Base):
    __tablename__ = "printers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    serial_number: Mapped[str] = mapped_column(String(50), unique=True)
    ip_address: Mapped[str] = mapped_column(String(253))
    access_code: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(50))
    # The place this printer stands in. A foreign key rather than a name, so a
    # rename reaches every printer, sensor and queued item at once — and so a
    # location that matches nothing can no longer be typed. See
    # models/printer_location.py for why it is not the spool-storage table.
    location_id: Mapped[int | None] = mapped_column(ForeignKey("printer_locations.id", ondelete="RESTRICT"))
    # selectin, not lazy: the printer list is read on every dashboard poll, and
    # a lazy load would be one extra query per printer on every one of them.
    location: Mapped["PrinterLocation | None"] = relationship(lazy="selectin")
    nozzle_count: Mapped[int] = mapped_column(default=1)  # 1 or 2, auto-detected from MQTT
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Soft-retire: archived printers disappear from the whole app + MQTT while
    # their print history is kept. Independent axis from is_active (Maintenance
    # Mode). See migration m105 + docs spec 2026-07-12-archived-printers.
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_archive: Mapped[bool] = mapped_column(Boolean, default=True)
    # Remove the print's files from the printer once it finishes — ours and the
    # copies the printer derives from them, on the card and in internal storage.
    #
    # ⚠️ **Default ON, and deliberately NOT backfilled.** The column default
    # applies to printers added from now on; every existing row keeps whatever
    # it has. A migration that flipped stored values would start deleting files
    # on farms that had chosen to keep them, silently, on the next print — the
    # one change here that cannot be undone. The API schema has defaulted this
    # to True for far longer than the column has, so printers added through the
    # UI already behave this way; this only makes the two agree.
    cleanup_after_print: Mapped[bool] = mapped_column(Boolean, default=True)
    # How long an MQTT connection is considered valid (seconds); 0 = disabled,
    # and disabled is the default. Above zero, ``ensure_fresh_connection``
    # discards the printer's client once the link is that old and builds a new
    # one with empty state — which is what let a swap-macro wait watch a
    # discarded connection and declare the macro failed, and what blanked the
    # skip-objects list mid-print. A link that genuinely drops still reconnects
    # through the normal connect loop; this only governs recycling a live one.
    # m120 zeroes the column on existing installations for the same reason.
    mqtt_connection_timeout: Mapped[int] = mapped_column(default=0)
    print_hours_offset: Mapped[float] = mapped_column(Float, default=0.0)  # Baseline hours to add
    runtime_seconds: Mapped[int] = mapped_column(default=0)  # Accumulated active runtime (RUNNING state only; #1521)
    last_runtime_update: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Last time runtime was updated
    # External camera configuration
    external_camera_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_camera_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # mjpeg, rtsp, snapshot
    external_camera_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional single-frame snapshot URL — when set, used for snapshot / finish-photo
    # / timelapse / plate-detect / Obico captures instead of opening the live stream
    # and skipping a warm-up frame. Bypasses MJPEG warm-up issues on sources that
    # expose a dedicated frame endpoint (e.g. go2rtc's /api/frame.jpeg). Upstream
    # Bambuddy #1177.
    external_camera_snapshot_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    camera_rotation: Mapped[int] = mapped_column(default=0)  # 0, 90, 180, 270 degrees
    # Plate detection - check if build plate is empty before starting print
    plate_detection_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # ROI for plate detection (percentages: 0.0-1.0)
    plate_detection_roi_x: Mapped[float | None] = mapped_column(Float, nullable=True)  # X start %
    plate_detection_roi_y: Mapped[float | None] = mapped_column(Float, nullable=True)  # Y start %
    plate_detection_roi_w: Mapped[float | None] = mapped_column(Float, nullable=True)  # Width %
    plate_detection_roi_h: Mapped[float | None] = mapped_column(Float, nullable=True)  # Height %
    # Staggered start: per-printer interval override (0 = use system default)
    stagger_interval_minutes: Mapped[int] = mapped_column(default=0)
    # Swap mode: A1 Mini plate swapper (swap-systems.com)
    swap_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Active swap-mode variant (catalog key from core/swap_profiles.py).
    # Null when swap_mode_enabled is False, or when swap is enabled without a
    # specific profile. Identifies which set of swap macros fires for this
    # printer (e.g. "a1mini_v1" vs "a1mini_v2" vs "a1").
    swap_profile: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Require user to confirm plate is cleared before next queued print starts
    require_plate_clear: Mapped[bool] = mapped_column(Boolean, default=True)
    # Persisted plate-clear gate: set True at print-end when require_plate_clear
    # is on; cleared when the user confirms or dispatch runs. Persisting it in
    # DB (vs the previous in-memory set) means Auto Off power cycles can't
    # silently bypass the confirmation (#961).
    awaiting_plate_clear: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    archives: Mapped[list["PrintArchive"]] = relationship(back_populates="printer", cascade="all, delete-orphan")
    smart_plugs: Mapped[list["SmartPlug"]] = relationship(back_populates="printer")
    notification_providers: Mapped[list["NotificationProvider"]] = relationship(back_populates="printer")
    maintenance_items: Mapped[list["PrinterMaintenance"]] = relationship(
        back_populates="printer", cascade="all, delete-orphan"
    )
    ams_history: Mapped[list["AMSSensorHistory"]] = relationship(back_populates="printer", cascade="all, delete-orphan")
    sensor_history: Mapped[list["PrinterSensorHistory"]] = relationship(
        back_populates="printer", cascade="all, delete-orphan"
    )
    queue: Mapped["PrinterQueue | None"] = relationship(  # noqa: F821
        back_populates="printer", uselist=False, cascade="all, delete-orphan"
    )


from backend.app.models.ams_history import AMSSensorHistory  # noqa: E402
from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.maintenance import PrinterMaintenance  # noqa: E402
from backend.app.models.notification import NotificationProvider  # noqa: E402
from backend.app.models.printer_sensor_history import PrinterSensorHistory  # noqa: E402, F401
from backend.app.models.smart_plug import SmartPlug  # noqa: E402
