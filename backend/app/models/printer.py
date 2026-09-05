from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

# Runtime import on purpose, not TYPE_CHECKING: ``Printer.location`` names
# "PrinterLocation" as a string, and configure_mappers() can only resolve it
# if the class reached the registry. Behind TYPE_CHECKING that depended on
# whoever happened to import printer_location first — fine in the app (init_db
# imports every model) but a landmine in any isolated test that touched the
# mapper registry. printer_location imports no models back, so no cycle.
from backend.app.models.printer_location import PrinterLocation  # noqa: F401

# Same reason: ``Printer.tags`` names "PrinterTag" as a string.
from backend.app.models.printer_tag import PrinterTag  # noqa: F401


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

    # Labels, resolved. selectin like ``location``: PrinterResponse.model_validate
    # reads this synchronously and a lazy hop there raises MissingGreenlet.
    # viewonly: the link rows are written by ``printer_tag_service.replace_links``,
    # never through this collection, so the ORM has no cascade to reason about.
    tags: Mapped[list["PrinterTag"]] = relationship(
        "PrinterTag", secondary="printer_tag_links", lazy="selectin", viewonly=True, order_by="PrinterTag.name_key"
    )

    @property
    def tag_ids(self) -> list[int]:
        """What a form posts back. A property so ``model_validate(printer)`` finds it —
        the list route validates straight off the ORM row."""
        return [tag.id for tag in self.tags]

    @property
    def plate_detection_roi(self) -> dict[str, float] | None:
        """The camera ROI as one object, or None when no component was ever set.

        A property for the same reason as ``tag_ids``: every printer response is
        validated straight off the row (``PrinterResponse.model_validate``), and
        the row carries only the four flat columns — so without this the nested
        field on the response was ``null`` for every printer that had one saved.
        Kept as a plain dict so Pydantic builds ``PlateDetectionROI`` from it.

        The per-component defaults are the ones the (now deleted)
        ``PrinterResponse.from_orm_with_roi`` applied, so a partially saved ROI
        keeps working. ``is not None`` rather than ``or``: a saved 0.0 is a
        legitimate edge of the frame, not a missing component.
        """
        parts = (
            self.plate_detection_roi_x,
            self.plate_detection_roi_y,
            self.plate_detection_roi_w,
            self.plate_detection_roi_h,
        )
        if all(p is None for p in parts):
            return None
        x, y, w, h = parts
        return {
            "x": x if x is not None else 0.15,
            "y": y if y is not None else 0.35,
            "w": w if w is not None else 0.70,
            "h": h if h is not None else 0.55,
        }

    nozzle_count: Mapped[int] = mapped_column(default=1)  # 1 or 2, auto-detected from MQTT
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Soft-retire: archived printers disappear from the whole app + MQTT while
    # their print history is kept. Independent axis from is_active (Maintenance
    # Mode). See migration m105 + docs spec 2026-07-12-archived-printers.
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # MQTT recording (m139). Persisted on purpose: the feature exists so a
    # capture outlives the window that started it, and a backend restart is only
    # a longer version of closing that window — the lifespan restarts whatever
    # is flagged here. Nothing caps the file, so ``started_at`` is what answers
    # "since when" about a recording nobody remembers switching on.
    mqtt_recording: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mqtt_recording_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    # No cascade on either of these two: a plug and a sensor are physical
    # hardware that outlive the printer they were wired to, so deleting the
    # printer unbinds them rather than deleting them.
    smart_sensors: Mapped[list["SmartSensor"]] = relationship(back_populates="printer")
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
from backend.app.models.printer_sensor_history import PrinterSensorHistory  # noqa: E402, F401
from backend.app.models.smart_plug import SmartPlug  # noqa: E402
from backend.app.models.smart_sensor import SmartSensor  # noqa: E402
