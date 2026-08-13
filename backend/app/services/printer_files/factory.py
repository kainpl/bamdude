"""The single place where a storage name becomes a transport."""

from __future__ import annotations

from backend.app.services.printer_files.base import PrinterFileTransport
from backend.app.services.printer_files.ftp import FtpTransport
from backend.app.services.printer_files.tunnel import TunnelTransport
from backend.app.utils.printer_storage import EXTERNAL, INTERNAL


def transport_for(printer, storage: str) -> PrinterFileTransport:
    """⚠️ Accepts our vocabulary only.

    A wire spelling (``emmc``, ``udisk``) arriving here means one leaked out of
    ``TunnelTransport``, so it is refused loudly rather than translated.
    """
    if storage == EXTERNAL:
        return FtpTransport(printer)
    if storage == INTERNAL:
        return TunnelTransport(printer)
    raise ValueError(f"unknown storage {storage!r}; expected {EXTERNAL!r} or {INTERNAL!r}")
