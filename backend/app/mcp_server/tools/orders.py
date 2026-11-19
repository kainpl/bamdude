"""Order reads reuse the normal Workshop ledger, never its mutation routes."""

from typing import Literal

from pydantic import Field

from backend.app.mcp_server.tools import Tool
from backend.app.services.farm_diagnostics.access import DiagnosticError
from backend.app.services.farm_diagnostics.contracts import PageInput, PositiveId


class SearchOrdersInput(PageInput):
    query: str = Field(default="", max_length=255)
    status: Literal["active", "completed", "cancelled", "any"] = "active"
    limit: int = Field(default=10, strict=True, ge=1, le=100)


class OrderProgressInput(PageInput):
    order_id: PositiveId
    line_id: PositiveId | None = None
    include_parts: bool = False
    limit: int = Field(default=10, strict=True, ge=1, le=50)


class OrderWorkInput(PageInput):
    order_id: PositiveId | None = None
    state: Literal["all", "running", "queued"] = "all"
    limit: int = Field(default=20, strict=True, ge=1, le=100)


TOOLS = (
    Tool(
        "bamdude_search_orders", "Find orders by literal name or order code; no filename inference.", SearchOrdersInput
    ),
    Tool(
        "bamdude_get_order_progress",
        "Read an order's canonical plan, QC provenance, stock receipts and remaining quantities. Whole-order access required.",
        OrderProgressInput,
    ),
    Tool(
        "bamdude_get_order_work",
        "Read order-linked running claims and pending work in both queues; claims are not physical print proof.",
        OrderWorkInput,
    ),
)
REQUIREMENTS = {
    "bamdude_search_orders": ("orders:read",),
    "bamdude_get_order_progress": ("orders:read", "products:read", "archives:read_all", "stock:read"),
    "bamdude_get_order_work": ("orders:read", "printers:read", "queue:read_all", "archives:read_all"),
}


def available(access, name):
    required = REQUIREMENTS.get(name)
    return bool(
        required and access.permits(*required) and (name != "bamdude_get_order_progress" or access.printer_ids is None)
    )


def visible(access):
    return tuple(tool for tool in TOOLS if available(access, tool.name))


async def read(name, access, args, request_id, *, owner=None):
    from backend.app.core import database
    from backend.app.services.order_diagnostics import order_progress, order_work, search_orders

    if not available(access, name):
        raise DiagnosticError("forbidden")
    reader = {
        "bamdude_search_orders": search_orders,
        "bamdude_get_order_progress": order_progress,
        "bamdude_get_order_work": order_work,
    }[name]
    async with database.async_session() as db:
        return await reader(db, access, args, request_id)
