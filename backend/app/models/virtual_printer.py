from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class VirtualPrinter(Base):
    """Virtual printer configuration for multi-instance support."""

    __tablename__ = "virtual_printers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="BamDude")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(20), default="file_manager")  # print_queue|auto_queue|file_manager|proxy
    auto_dispatch: Mapped[bool] = mapped_column(Boolean, default=True)  # print_queue + auto_queue: auto-start or manual
    # Per-VP toggle (#1188): when True, the auto-queue intake auto-extracts
    # per-slot type+color from every incoming 3MF and writes them as
    # force_color_match=True overrides on the new auto_queue_items row, so
    # the eligibility scheduler refuses to route a job onto a printer with
    # the right material but wrong colour. Default False for upgrader
    # compatibility — existing AutoQueue installs keep the legacy
    # types-only matching unless the operator flips this on per-VP.
    queue_force_color_match: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Per-VP opt-in for auto-print G-code injection (#1516). When True, files this
    # VP queues (print_queue or auto_queue mode) carry gcode_injection=True so the
    # dispatcher splices the per-model start/end snippets. Default off; no-op unless
    # gcode_snippets exist for the target model.
    gcode_injection: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    model: Mapped[str | None] = mapped_column(String(50), nullable=True)  # SSDP model code (server mode)
    access_code: Mapped[str | None] = mapped_column(String(8), nullable=True)  # 8 chars (server mode)
    target_printer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("printers.id", ondelete="SET NULL"), nullable=True
    )  # proxy mode
    # Per-VP destination folder for files arriving via FTP. NULL = library root.
    # Used by the post-Audit-2 redesign that saves incoming files to the
    # library + queues them with library_file_id instead of pre-creating an
    # archived-status placeholder row. SET NULL on folder delete so the VP
    # keeps working (falls back to root).
    target_folder_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("library_folders.id", ondelete="SET NULL"), nullable=True
    )
    bind_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # dedicated IP (proxy mode)
    remote_interface_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # SSDP advertise IP
    serial_suffix: Mapped[str] = mapped_column(String(9), default="391800001")  # unique per printer
    # Tailscale per-VP toggle (#1070). INFORMATIONAL ONLY since the LE-cert
    # rip-out: when False (= integration on) the VP card fetches
    # /virtual-printers/tailscale-status and shows the host's tailnet IP +
    # MagicDNS name to paste into the slicer. It does NOT touch certificates,
    # SSDP advertisement, or any service lifecycle — flipping it must not
    # restart the VP (see manager.sync_from_db). Defaults to True (opt-in) —
    # most installs don't have Tailscale. NOTE: m030's docstring still
    # describes the old LE-cert behaviour; migrations are frozen, so this
    # comment is the current source of truth.
    tailscale_disabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
