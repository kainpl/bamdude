from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class VirtualPrinter(Base):
    """Virtual printer configuration for multi-instance support."""

    __tablename__ = "virtual_printers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="Bambuddy")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(20), default="file_manager")  # print_queue|auto_queue|file_manager|proxy
    auto_dispatch: Mapped[bool] = mapped_column(Boolean, default=True)  # print_queue + auto_queue: auto-start or manual
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
    # Tailscale per-VP toggle (#1070): when False, manager.py asks the local
    # ``tailscale`` CLI for an LE cert and advertises the tailnet FQDN over
    # SSDP so slicers connect via a hostname that matches the trusted cert.
    # Defaults to True (opt-in) — most installs don't have Tailscale.
    tailscale_disabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
