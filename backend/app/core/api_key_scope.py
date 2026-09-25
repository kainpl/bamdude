"""An API key's printer allowlist, held on every route that names a printer.

``APIKey.printer_ids`` — ``None`` is every printer, ``[]`` is none — was taken,
stored and shown, and honoured by the ``/webhook/*`` routes alone, so a key
"limited to printer 1" read, controlled and queued for printer 2 through
everything else. The permission gates in ``core/auth.py`` now call
:func:`enforce_printer_scope` right after a key's scope flags pass, so no route
can forget it. It collects every printer the request names:

* ``printer_id`` / ``printer_ids`` / ``queue_id`` in the path, the query, or
  **anywhere in a JSON body** — a printer's queue id IS its printer id — and
  the support bundle's comma-separated ``mqtt_printer_ids``. FastAPI parses the body
  before it solves dependencies and caches it on the request, so reading it
  here costs nothing and runs before any check of the route's own, and a new
  route that takes a printer in its body is covered without being touched;
* queued work: ``source_queue_item_id`` / ``queue_item_ids`` anywhere, and on
  ``/queue/…`` also ``{item_id}``, ``{batch_id}``, ``item_ids`` and
  ``items[].id`` — each through the job's queue;
* the things that act on one printer: a smart plug, a maintenance item, a
  dispatch job, a calibration session.

A name that resolves to nothing is let through — the route answers its own 404.
A value that cannot be read as an id is refused: pydantic would coerce ``"2"``
or ``2.0`` to printer 2, so anything this reader cannot interpret must not pass.
The auto-queue is farm-wide — its distributor may hand work to any printer — so
it is closed to a restricted key as a whole.

Lists are the routes' own business: :func:`key_printer_scope` says which
printers the caller may see (``None`` = no restriction), and the printers,
statuses, queues, queue items and monitor lists narrow to it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.maintenance import PrinterMaintenance
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying
from backend.app.models.smart_plug import SmartPlug

_STATE_ATTR = "api_key_printer_ids"

# Names that are a printer (or its queue) wherever they appear.
_PRINTER_KEYS = frozenset({"printer_id", "printer_ids", "queue_id"})
# Query parameters that carry printers as one comma-separated string. Parsed the
# way the route parses them (digits only, the rest ignored), so nothing it acts
# on can slip past.
_PRINTER_CSV_QUERY = frozenset({"mqtt_printer_ids"})
# Names that are a print-queue job wherever they appear.
_QUEUE_ITEM_KEYS = frozenset({"source_queue_item_id", "queue_item_ids"})
# Only on the queue router are these print-queue jobs; elsewhere they are other tables' ids.
_QUEUE_ROUTER = "/api/v1/queue/"
_QUEUE_ROUTER_ITEM_KEYS = frozenset({"item_ids"})
_FARM_WIDE_ROUTERS = ("/api/v1/auto-queue",)


class _Unreadable(Exception):
    """A printer reference this reader cannot interpret."""


def printer_refusal(printer_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"API key does not have access to printer {printer_id}",
    )


def key_printer_scope(request: Request | None) -> frozenset[int] | None:
    """The printers an API-key caller may reach; ``None`` for a user or an unrestricted key.

    ``request`` is None only when a route function is called directly from
    Python — FastAPI always injects it — and such a call carries no key.
    """
    if request is None:
        return None
    return getattr(request.state, _STATE_ATTR, None)


def in_key_scope(request: Request | None, printer_id: int | None) -> bool:
    scope = key_printer_scope(request)
    return scope is None or printer_id in scope


def _allowed(api_key: Any) -> frozenset[int] | None:
    if api_key.printer_ids is None:
        return None
    return frozenset(int(pid) for pid in api_key.printer_ids)


def _as_id(value: Any) -> int:
    if isinstance(value, bool):
        raise _Unreadable
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                raise _Unreadable from None
            if number.is_integer():
                return int(number)
    raise _Unreadable


def _ids(value: Any) -> Iterator[int]:
    """A scalar id or a list of them; ``None`` names nothing."""
    if value is None:
        return
    for one in value if isinstance(value, list) else [value]:
        if one is not None:
            yield _as_id(one)


def _find(node: Any, keys: frozenset[str]) -> Iterator[Any]:
    """Every value stored under one of ``keys``, at any depth of a JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                yield value
            yield from _find(value, keys)
    elif isinstance(node, list):
        for value in node:
            yield from _find(value, keys)


async def _json_body(request: Request) -> Any:
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        return None
    try:
        return await request.json()
    except ValueError:
        return None  # the route answers the malformed body itself


async def _queues_of_items(db: AsyncSession, item_ids: set[int]) -> set[int]:
    if not item_ids:
        return set()
    rows = await db.execute(select(PrintQueueItem.queue_id).where(PrintQueueItem.id.in_(item_ids)))
    return set(rows.scalars().all())


async def _printer_of(db: AsyncSession, model: Any, raw: str) -> set[int]:
    printer_id = await db.scalar(select(model.printer_id).where(model.id == _as_id(raw)))
    return {printer_id} if printer_id is not None else set()


async def _queue_item(db: AsyncSession, raw: str) -> set[int]:
    return await _queues_of_items(db, {_as_id(raw)})


async def _queue_batch(db: AsyncSession, raw: str) -> set[int]:
    rows = await db.execute(select(PrintQueueItem.queue_id).where(PrintQueueItem.batch_id == raw))
    return set(rows.scalars().all())


async def _smart_plug(db: AsyncSession, raw: str) -> set[int]:
    return await _printer_of(db, SmartPlug, raw)


async def _maintenance_item(db: AsyncSession, raw: str) -> set[int]:
    return await _printer_of(db, PrinterMaintenance, raw)


async def _calibration_session(db: AsyncSession, raw: str) -> set[int]:
    return await _printer_of(db, CalibrationSession, raw)


async def _scheduled_drying_run(db: AsyncSession, raw: str) -> set[int]:
    return await _printer_of(db, ScheduledDrying, raw)


async def _drying_schedule(db: AsyncSession, raw: str) -> set[int]:
    return await _printer_of(db, DryingSchedule, raw)


async def _dispatch_job(_db: AsyncSession, raw: str) -> set[int]:
    from backend.app.services.background_dispatch import background_dispatch

    printer_id = background_dispatch.printer_of_job(_as_id(raw))
    return {printer_id} if printer_id is not None else set()


# (route template prefix, path parameter) → the printers that id belongs to.
_PATH_RESOLVERS: tuple[tuple[str, str, Callable[[AsyncSession, str], Awaitable[set[int]]]], ...] = (
    (_QUEUE_ROUTER, "item_id", _queue_item),
    (_QUEUE_ROUTER, "batch_id", _queue_batch),
    ("/api/v1/smart-plugs/", "plug_id", _smart_plug),
    ("/api/v1/maintenance/items/", "item_id", _maintenance_item),
    ("/api/v1/calibration/sessions/", "session_id", _calibration_session),
    ("/api/v1/background-dispatch/", "job_id", _dispatch_job),
    ("/api/v1/scheduled-dryings/", "run_id", _scheduled_drying_run),
    ("/api/v1/drying-schedules/", "schedule_id", _drying_schedule),
)


async def printers_named_by(db: AsyncSession, request: Request, template: str) -> set[int]:
    """Every printer id the request names, directly or through something that belongs to one."""
    printers: set[int] = set()
    items: set[int] = set()
    for key in _PRINTER_KEYS:
        if key in request.path_params:
            printers.update(_ids(request.path_params[key]))
        for value in request.query_params.getlist(key):
            printers.update(_ids(value))
    for key in _PRINTER_CSV_QUERY:
        for value in request.query_params.getlist(key):
            printers.update(int(token.strip()) for token in value.split(",") if token.strip().isdigit())

    for prefix, param, resolve in _PATH_RESOLVERS:
        if template.startswith(prefix) and param in request.path_params:
            printers |= await resolve(db, request.path_params[param])

    body = await _json_body(request)
    for value in _find(body, _PRINTER_KEYS):
        printers.update(_ids(value))
    for value in _find(body, _QUEUE_ITEM_KEYS):
        items.update(_ids(value))
    if template.startswith(_QUEUE_ROUTER):
        for value in _find(body, _QUEUE_ROUTER_ITEM_KEYS):
            items.update(_ids(value))
        # ``/queue/reorder`` names its jobs as ``items: [{"id": …, "position": …}]``.
        for entries in _find(body, frozenset({"items"})):
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and "id" in entry:
                    items.update(_ids(entry["id"]))

    return printers | await _queues_of_items(db, items)


async def enforce_printer_scope(db: AsyncSession, request: Request, api_key: Any) -> None:
    """Refuse a restricted key any request that names a printer outside its list.

    Also records the key's list on the request for the routes that narrow a
    list to it (:func:`key_printer_scope`).
    """
    allowed = _allowed(api_key)
    setattr(request.state, _STATE_ATTR, allowed)
    if allowed is None:
        return

    template = getattr(request.scope.get("route"), "path", request.url.path)
    if any(template == router or template.startswith(router + "/") for router in _FARM_WIDE_ROUTERS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An API key restricted to specific printers cannot use the farm-wide auto-queue",
        )

    try:
        named = await printers_named_by(db, request, template)
    except _Unreadable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An API key restricted to specific printers must name a printer by its id",
        ) from None
    for printer_id in sorted(named):
        if printer_id not in allowed:
            raise printer_refusal(printer_id)
