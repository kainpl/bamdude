"""Schemas for virtual printer diagnostics."""

from typing import Literal

from pydantic import BaseModel

from backend.app.schemas.printer import DiagnosticCheck

VPNozzleDiameter = Literal["0.2", "0.4", "0.6", "0.8"]
VPNozzleFlow = Literal["standard", "high_flow", "tpu_high_flow", "e3d_high_flow"]


class VPDiagnosticResult(BaseModel):
    """Result of a virtual-printer setup diagnostic run.

    Mirrors ``PrinterDiagnosticResult`` but keyed to a virtual printer: the
    checks probe the VP's own bind IP and local services rather than a remote
    printer. ``checks[].id`` values are VP-specific (enabled, running,
    bind_interface, access_code, target_printer, port_ftps, port_mqtt,
    port_bind, certificate); the frontend renders the localized title and
    fix text from id + status.
    """

    vp_id: int
    vp_name: str
    mode: str
    overall: str  # "ok" | "warnings" | "problems"
    checks: list[DiagnosticCheck]
