"""Thin authorization/serialization boundary for the implemented B readers."""

from backend.app.mcp_server.contracts import EmptyInput
from backend.app.mcp_server.tools import Tool
from backend.app.services.farm_diagnostics.access import available, require
from backend.app.services.farm_diagnostics.contracts import InspectInput, PageInput

TOOLS = (
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
)


def visible(access):
    return tuple(tool for tool in TOOLS if available(access, tool.name))


async def read(name, access, args, request_id):
    from backend.app.core import database
    from backend.app.services.farm_diagnostics.health import system_health
    from backend.app.services.farm_diagnostics.overview import overview
    from backend.app.services.farm_diagnostics.printer import inspect_printer

    require(access, name)
    if name == "bamdude_get_system_health":
        return await system_health(access, args, request_id)
    async with database.async_session() as db:
        if name == "bamdude_get_farm_overview":
            return await overview(db, access, args, request_id)
        return await inspect_printer(db, access, args, request_id)
