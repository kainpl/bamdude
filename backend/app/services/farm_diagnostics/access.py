"""Read-only grants within the existing request's key/owner ceiling."""

from sqlalchemy import true

from backend.app.mcp_server.contracts import DiagnosticAccess

RECOVERY_READ = frozenset({"system:read", "printers:read", "queue:read_all", "archives:read_all"})
REQUIREMENTS = {
    "bamdude_get_farm_overview": frozenset({"printers:read"}),
    "bamdude_inspect_printer": frozenset({"printers:read"}),
    "bamdude_get_system_health": frozenset({"system:read"}),
    "bamdude_get_print_timeline": frozenset({"printers:read", "archives:read_all"}),
    "bamdude_read_logs": frozenset({"settings:read", "printers:read"}),
    "bamdude_inspect_energy": frozenset({"printers:read", "archives:read_all", "smart_plugs:read", "stats:read"}),
    "bamdude_list_state_issues": RECOVERY_READ,
    "bamdude_explain_recovery": RECOVERY_READ,
    "bamdude_get_period_summary": frozenset({"printers:read", "archives:read_all", "stats:read"}),
}


class DiagnosticError(Exception):
    """Only fixed public error codes cross the tool boundary."""


def available(access: DiagnosticAccess, name: str) -> bool:
    required = REQUIREMENTS.get(name)
    if required is None or not access.permits(*required):
        return False
    if name in {"bamdude_get_system_health", "bamdude_read_logs"} and access.printer_ids is not None:
        return False
    return name != "bamdude_read_logs" or access.logs_allowed


def require(access: DiagnosticAccess, name: str) -> None:
    if not available(access, name):
        raise DiagnosticError("forbidden")


def printer_scope(access: DiagnosticAccess, column):
    # None and empty are intentionally different, including before aggregates.
    return true() if access.printer_ids is None else column.in_(sorted(access.printer_ids))
