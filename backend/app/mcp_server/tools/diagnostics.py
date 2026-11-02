"""Thin authorization/serialization boundary for the implemented B readers."""

from backend.app.mcp_server.contracts import EmptyInput
from backend.app.mcp_server.tools import Tool
from backend.app.services.farm_diagnostics.access import available, require
from backend.app.services.farm_diagnostics.contracts import (
    EnergyInput,
    InspectInput,
    LogsInput,
    PageInput,
    TimelineInput,
)

TOOLS = (
    Tool(
        "bamdude_inspect_energy",
        "Read scoped persisted print energy and current-plug counter evidence; never poll hardware.",
        EnergyInput,
    ),
    Tool("bamdude_get_farm_overview", "Read a scoped page of cached printer and queue observations.", PageInput),
    Tool(
        "bamdude_inspect_printer",
        "Read one visible printer without polling hardware or capturing images.",
        InspectInput,
    ),
    Tool(
        "bamdude_get_system_health",
        "Read allowlisted local worker and database-pool state without active probes.",
        EmptyInput,
    ),
    Tool(
        "bamdude_get_print_timeline",
        "Read archive-owned timestamps and retained events, never correlate by filename.",
        TimelineInput,
    ),
    Tool(
        "bamdude_read_logs",
        "Read a bounded sanitized application-log tail. Unknown secrets may remain; log opt-in required.",
        LogsInput,
    ),
)


def visible(access):
    return tuple(tool for tool in TOOLS if available(access, tool.name))


async def read(name, access, args, request_id, *, owner=None):
    from backend.app.core import database
    from backend.app.services.farm_diagnostics.energy import inspect_energy
    from backend.app.services.farm_diagnostics.health import system_health
    from backend.app.services.farm_diagnostics.logs import read_logs
    from backend.app.services.farm_diagnostics.overview import overview
    from backend.app.services.farm_diagnostics.printer import inspect_printer
    from backend.app.services.farm_diagnostics.timeline import timeline

    require(access, name)
    if name == "bamdude_get_system_health":
        return await system_health(access, args, request_id)
    if name == "bamdude_read_logs":
        return await read_logs(access, args, request_id, owner)
    async with database.async_session() as db:
        if name == "bamdude_inspect_energy":
            return await inspect_energy(db, access, args, request_id)
        if name == "bamdude_get_farm_overview":
            return await overview(db, access, args, request_id)
        if name == "bamdude_get_print_timeline":
            return await timeline(db, access, args, request_id)
        return await inspect_printer(db, access, args, request_id)
