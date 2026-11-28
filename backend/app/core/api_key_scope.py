"""The caller's printer scope, held on every route that names a printer.

N62 (spec vault ``60-specs/n62-printer-scoped-groups-spec.md``): this began as an API key's
allowlist and now holds EVERY caller — a person limited by their groups, an API key by its
list ∩ its owner's scope (``core/printer_scope``). The gates call :func:`enforce_person_scope`
or :func:`enforce_key_scope`; a person is refused with the 404 of the thing named (as if it
did not exist), a key with its 403 (the connector plugin and webhooks read it, spec §12.1).
When the caller reaches every printer nothing here runs.

The history below is the key's:

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

Lists are the routes' own business: ``core/printer_scope.request_printer_scope``
says which printers the caller may see (``None`` = no restriction), and every list
narrows to it.

⚠️ Only a JSON body is read. No route takes a printer in a form field today; the
coverage guard fails one that does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.printer_scope import (
    Scope,
    key_list,
    key_scope,
    request_key_listed,
    request_printer_scope,
    request_scope_kind,
    set_request_scope,
    user_scope,
)
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.maintenance import MaintenanceHistory, PrinterMaintenance
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

# Names that are a printer (or its queue) wherever they appear.
_PRINTER_KEYS = frozenset({"printer_id", "printer_ids", "queue_id"})
# Query parameters that carry printers as one comma-separated string. Parsed the
# way the route parses them (digits only, the rest ignored), so nothing it acts
# on can slip past.
_PRINTER_CSV_QUERY = frozenset({"mqtt_printer_ids"})
# Names that are a print-queue job wherever they appear.
_QUEUE_ITEM_KEYS = frozenset({"source_queue_item_id", "queue_item_ids"})
# Names that are print archives wherever they appear in a request body (an order's add / remove
# archives, N62 r1 B04; the archive a queue / Auto Queue job or a routing preview prints from,
# acceptance A01): an archive belongs to the printer that printed it.
_ARCHIVE_KEYS = frozenset({"archive_ids", "archive_id"})
# Only on the queue router are these print-queue jobs; elsewhere they are other tables' ids.
# ``cancelled_item_ids`` (bulk-retry) is read here itself rather than left to its body validator
# (cancelled ⊆ confirmed): the gate runs before the body is validated, and a guard that depends on a
# schema rule elsewhere stops guarding the day that rule is relaxed.
# ``after_item_ids`` (next-block, N03) only orders the new block behind a run's own rows, but a
# job list is a printer list once removed, so the gate reads it like the others.
_QUEUE_ROUTER = "/api/v1/queue/"
_QUEUE_ROUTER_ITEM_KEYS = frozenset({"item_ids", "cancelled_item_ids", "after_item_ids"})
_FARM_WIDE_ROUTERS = ("/api/v1/auto-queue",)
# The Workshop's doors onto the same distributor (WS-13 E13 O14): a line's rebalance always,
# an order plan sent to the auto-queue (``target.kind == "auto"``) — the distributor picks the printer.
_FARM_WIDE_ROUTES = frozenset(
    {
        "/api/v1/projects/{project_id}/lines/{line_id}/rebalance",
        "/api/v1/queue/{item_id}/auto-transfer-preview",
        "/api/v1/queue/{item_id}/move-to-auto",
    }
)
_FARM_WIDE_WHEN_AUTO = frozenset({"/api/v1/projects/{project_id}/plan/enqueue"})
# The slicer connector (OrcaBamDude spec r2 §7.2, addendum C2). A new operation names its targets in
# ``entries[].target``: any AutoQueue entry is farm-wide. An existing one is addressed by its id alone; its targets
# come from the stored manifest of THIS key's own namespace -- a foreign or missing operation names nothing here and
# the route answers 404, so no 403 tells one from the other.
_CONNECTOR_PREFIX = "/api/v1/connector/"
_CONNECTOR_SUBMIT = "/api/v1/connector/submissions"
_CONNECTOR_OPERATION_ROUTES = frozenset(
    {
        "/api/v1/connector/submissions/{operation_id}",
        "/api/v1/connector/submissions/{operation_id}/resume",
        "/api/v1/connector/submissions/{operation_id}/cancel",
    }
)


class _Unreadable(Exception):
    """A printer reference this reader cannot interpret."""


def printer_refusal(printer_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"API key does not have access to printer {printer_id}",
    )


# What a person is told about a printer outside their scope, by what named it: the thing's own
# 404, so another team's number is never confirmed (N62 spec §5.1).
_PRINTER = "Printer not found"
_NOT_FOUND = {
    "printer": _PRINTER,
    "queue_item": "Queue item not found",
    "batch": "Batch not found",
    "smart_plug": "Smart plug not found",
    "maintenance_item": "Maintenance item not found",
    # N09 (acceptance A1-01): the route's own refusal, so another printer's entry reads as none.
    "maintenance_history_entry": "Maintenance history entry not found",
    "calibration_session": "Calibration session not found",
    "dispatch_job": "Job not found",
    "drying_run": "Scheduled drying not found",
    "drying_schedule": "Drying schedule not found",
    "archive": "Archive not found",
    "calibration": "Calibration not found",
    "spoolman_spool": "Spool assignment not found",
    "auto_queue_item": "Auto-queue item not found",
}


def person_refusal(source: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND.get(source, _PRINTER))


async def _place_printers(db: AsyncSession, printer_location_id: int, cache: dict | None) -> set[int]:
    """The printers standing in a printer location or in one below it."""
    if cache is not None and printer_location_id in cache:
        return cache[printer_location_id]
    from backend.app.core.printer_scope import _location_subtree
    from backend.app.models.printer import Printer

    places = await _location_subtree(db, [printer_location_id])
    printers = set((await db.execute(select(Printer.id).where(Printer.location_id.in_(places)))).scalars())
    if cache is not None:
        cache[printer_location_id] = printers
    return printers


async def target_in_request_scope(
    request: Request,
    db: AsyncSession,
    *,
    printer_id: int | None = None,
    printer_location_id: int | None = None,
    write: bool = False,
    cache: dict | None = None,
) -> bool:
    """May this request see (``write`` — change) a sensor target? One rule (spec §5.2, §12.2):

    * bound to a printer — that printer is in scope;
    * bound to a printer location — reading needs a printer of the place (or a place below it)
      in scope; changing needs every printer there in scope, and at least one;
    * a storage place, or no target — nobody's printer: visible, like the spools (r1 B06);
    * a key with a printer list of its OWN keeps its older rule — a sensor only through a
      printer target.
    """
    scope = request_printer_scope(request)
    if scope is None:
        return True
    if printer_id is not None:
        return printer_id in scope
    if request_key_listed(request):
        return False
    if printer_location_id is not None:
        printers = await _place_printers(db, printer_location_id, cache)
        return bool(printers) and printers <= scope if write else bool(printers & scope)
    return True


async def sensor_in_request_scope(request: Request, db: AsyncSession, sensor_id: int, *, write: bool = False) -> bool:
    """A sensor as a whole: seen through any of its targets; changed only when every one of them
    may be changed — a sensor shared with another team's printers is theirs too."""
    if request_printer_scope(request) is None:
        return True
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.smart_sensor_binding import SmartSensorBinding

    targets = (
        await db.execute(
            select(SmartSensorBinding.printer_id, SmartSensorBinding.printer_location_id).where(
                SmartSensorBinding.sensor_id == sensor_id
            )
        )
    ).all()
    if not targets:
        targets = (
            await db.execute(select(SmartSensor.printer_id, SmartSensor.location_id).where(SmartSensor.id == sensor_id))
        ).all()
    cache: dict = {}
    answers = [
        await target_in_request_scope(
            request, db, printer_id=printer_id, printer_location_id=place_id, write=write, cache=cache
        )
        for printer_id, place_id in targets
    ]
    if not answers:
        return await target_in_request_scope(request, db, write=write)
    return all(answers) if write else any(answers)


def sensor_refusal(request: Request, what: str = "sensor") -> HTTPException:
    """A person is told the sensor (``what``: ``sensor`` / ``binding`` / ``target``) is not there —
    404, as for anything outside their printers; a key that it may not reach it — 403, as before."""
    person = request_scope_kind(request) == "person"
    if what == "binding":
        if person:
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such sensor binding.")
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API key does not have access to this sensor binding."
        )
    if what == "target":
        if person:
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such binding target.")
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="API key does not have access to that target."
        )
    if person:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such sensor.")
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key does not have access to this sensor.")


def narrowed_scope(granted: frozenset[int] | None, api_key: Any) -> frozenset[int] | None:
    """The request's printer scope narrowed by the key's printer list as it stands NOW -- for a writer that reads
    the key again inside its transaction. Only ever narrower than what the gate granted, never wider."""
    current = key_list(api_key)
    if granted is None:
        return current
    return granted if current is None else granted & current


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


async def _queue_batch(db: AsyncSession, raw: str, within: set[int] | None = None) -> set[int]:
    """The queues of a batch's rows -- every queue it touches, unless the request names its queue.

    A batch route acts on ONE printer queue's rows (G03 D1): AutoQueue promotion spreads a batch over the
    printers that took its copies, and ``?queue_id=`` names whose copies the route touches. With it, the
    rows acted on are the batch's rows in that queue, so that is what the key's list is asked of -- the
    named queue itself is read as a printer reference beside this. Without it the route may still act on
    the batch's only queue, so every queue the batch touches is asked, as strictly as before.
    """
    stmt = select(PrintQueueItem.queue_id).where(PrintQueueItem.batch_id == raw)
    if within is not None:
        stmt = stmt.where(PrintQueueItem.queue_id.in_(within))
    rows = await db.execute(stmt)
    return set(rows.scalars().all())


async def _smart_plug(db: AsyncSession, raw: str) -> set[int]:
    return await _printer_of(db, SmartPlug, raw)


async def _maintenance_item(db: AsyncSession, raw: str) -> set[int]:
    return await _printer_of(db, PrinterMaintenance, raw)


async def _maintenance_history_entry(db: AsyncSession, raw: str) -> set[int]:
    # N09: an entry of a deleted printer names no printer; the route's own check decides it.
    return await _printer_of(db, MaintenanceHistory, raw)


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


async def _archives(db: AsyncSession, archive_ids: set[int]) -> set[int]:
    """The printers of these archives; an archive bound to no printer names none (N62 D1)."""
    if not archive_ids:
        return set()
    rows = await db.execute(select(PrintArchive.printer_id).where(PrintArchive.id.in_(archive_ids)))
    return {printer_id for printer_id in rows.scalars() if printer_id is not None}


async def _archive(db: AsyncSession, raw: str) -> set[int]:
    return await _archives(db, {_as_id(raw)})


async def _calibration(db: AsyncSession, raw: str) -> set[int]:
    return await _printer_of(db, FilamentCalibration, raw)


async def _spoolman_spool(db: AsyncSession, raw: str) -> set[int]:
    """Every printer whose slot holds this Spoolman spool — an unassign by the spool's number acts on them."""
    rows = await db.execute(
        select(SpoolmanSlotAssignment.printer_id).where(SpoolmanSlotAssignment.spoolman_spool_id == _as_id(raw))
    )
    return set(rows.scalars())


async def _auto_queue_item(db: AsyncSession, raw: str) -> set[int]:
    """The printer an Auto Queue row was handed to (its copy's queue IS that printer's number);
    a row still waiting names none (N62 12.3, acceptance A03)."""
    queue_id = await db.scalar(
        select(PrintQueueItem.queue_id)
        .join(AutoQueueItem, AutoQueueItem.assigned_to_item_id == PrintQueueItem.id)
        .where(AutoQueueItem.id == _as_id(raw))
    )
    return {queue_id} if queue_id is not None else set()


async def _auto_queue_batch(db: AsyncSession, raw: str) -> set[int]:
    """Every printer the copies of an Auto Queue batch were handed to."""
    rows = await db.execute(
        select(PrintQueueItem.queue_id)
        .join(AutoQueueItem, AutoQueueItem.assigned_to_item_id == PrintQueueItem.id)
        .where(AutoQueueItem.batch_id == raw)
    )
    return {queue_id for queue_id in rows.scalars() if queue_id is not None}


# (route template prefix, path parameter, what it is) → the printers that id belongs to.
_PATH_RESOLVERS: tuple[tuple[str, str, Callable[[AsyncSession, str], Awaitable[set[int]]], str], ...] = (
    (_QUEUE_ROUTER, "item_id", _queue_item, "queue_item"),
    (_QUEUE_ROUTER, "batch_id", _queue_batch, "batch"),
    ("/api/v1/smart-plugs/", "plug_id", _smart_plug, "smart_plug"),
    ("/api/v1/maintenance/items/", "item_id", _maintenance_item, "maintenance_item"),
    ("/api/v1/maintenance/history/", "entry_id", _maintenance_history_entry, "maintenance_history_entry"),
    ("/api/v1/calibration/sessions/", "session_id", _calibration_session, "calibration_session"),
    ("/api/v1/background-dispatch/", "job_id", _dispatch_job, "dispatch_job"),
    ("/api/v1/scheduled-dryings/", "run_id", _scheduled_drying_run, "drying_run"),
    ("/api/v1/drying-schedules/", "schedule_id", _drying_schedule, "drying_schedule"),
    # N62 (D1, D5): history belongs to the printer that made it, so an archive named in a path is
    # asked of its printer — under the archive routes and everywhere else that names one.
    ("/api/v1/", "archive_id", _archive, "archive"),
    ("/api/v1/filament-calibrations/", "cali_id", _calibration, "calibration"),
    ("/api/v1/spoolman/inventory/slot-assignments/", "spoolman_spool_id", _spoolman_spool, "spoolman_spool"),
    ("/api/v1/auto-queue/", "item_id", _auto_queue_item, "auto_queue_item"),
    ("/api/v1/auto-queue/", "batch_id", _auto_queue_batch, "batch"),
)


async def printers_named_by(
    db: AsyncSession, request: Request, template: str, sources: dict[int, str] | None = None
) -> set[int]:
    """Every printer id the request names, directly or through something that belongs to one.

    ``sources`` (when given) learns, for each printer, what named it first — the person's 404
    names that thing.
    """
    printers: set[int] = set()
    items: set[int] = set()

    def named(found: set[int] | Iterator[int], source: str) -> None:
        for printer_id in found:
            printers.add(printer_id)
            if sources is not None:
                sources.setdefault(printer_id, source)

    for key in _PRINTER_KEYS:
        if key in request.path_params:
            named(_ids(request.path_params[key]), "printer")
        for value in request.query_params.getlist(key):
            named(_ids(value), "printer")
    for key in _PRINTER_CSV_QUERY:
        for value in request.query_params.getlist(key):
            named({int(token.strip()) for token in value.split(",") if token.strip().isdigit()}, "printer")
    # The queue a batch route is told to act on (``?queue_id=``, G03 D1), already checked above as a printer.
    named_queues = {one for value in request.query_params.getlist("queue_id") for one in _ids(value)}

    for prefix, param, resolve, source in _PATH_RESOLVERS:
        if template.startswith(prefix) and param in request.path_params:
            if resolve is _queue_batch and named_queues:
                named(await _queue_batch(db, request.path_params[param], within=named_queues), source)
            else:
                named(await resolve(db, request.path_params[param]), source)

    body = await _json_body(request)
    for value in _find(body, _PRINTER_KEYS):
        named(_ids(value), "printer")
    for value in _find(body, _QUEUE_ITEM_KEYS):
        items.update(_ids(value))
    archives: set[int] = set()
    for value in _find(body, _ARCHIVE_KEYS):
        archives.update(_ids(value))
    # ``/archives/compare?archive_ids=1,2,3``: a token that is not a number names nothing — the
    # route answers the malformed list itself (400).
    for key in _ARCHIVE_KEYS:
        for value in request.query_params.getlist(key):
            archives.update(int(token.strip()) for token in value.split(",") if token.strip().isdigit())
    if template.startswith(_QUEUE_ROUTER):
        for value in _find(body, _QUEUE_ROUTER_ITEM_KEYS):
            items.update(_ids(value))
        # ``/queue/reorder`` names its jobs as ``items: [{"id": …, "position": …}]``.
        for entries in _find(body, frozenset({"items"})):
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and "id" in entry:
                    items.update(_ids(entry["id"]))

    named(await _queues_of_items(db, items), "queue_item")
    named(await _archives(db, archives), "archive")
    return printers


async def enforce_person_scope(db: AsyncSession, request: Request, user: Any) -> Scope:
    """Refuse a restricted person any request that names a printer outside their scope — with the
    404 of the thing named, as if it did not exist (N62 spec §5.1).

    Also records the scope on the request for the routes that narrow a list to it
    (``core/printer_scope.request_printer_scope``). The Auto Queue is open to a person (D2): the
    distributor holds the job to its author's scope.
    """
    scope = await user_scope(db, user)
    set_request_scope(request, scope, "person")
    if scope is None:
        return None
    template = getattr(request.scope.get("route"), "path", request.url.path)
    sources: dict[int, str] = {}
    try:
        named = await printers_named_by(db, request, template, sources)
    except _Unreadable:
        raise person_refusal("printer") from None
    for printer_id in sorted(named):
        if printer_id not in scope:
            raise person_refusal(sources.get(printer_id, "printer"))
    return scope


async def enforce_key_scope(db: AsyncSession, request: Request, api_key: Any, owner: Any) -> Scope:
    """Refuse a restricted key any request that names a printer outside its scope — its own list ∩
    its owner's scope (D5).

    Also records the scope on the request for the routes that narrow a list to it.
    """
    allowed = await key_scope(db, api_key, owner)
    set_request_scope(request, allowed, "key", key_listed=key_list(api_key) is not None)
    if allowed is None:
        return None

    template = getattr(request.scope.get("route"), "path", request.url.path)
    farm_wide = template in _FARM_WIDE_ROUTES or any(
        template == router or template.startswith(router + "/") for router in _FARM_WIDE_ROUTERS
    )
    if not farm_wide and template in _FARM_WIDE_WHEN_AUTO:
        body = await _json_body(request)
        target = body.get("target") if isinstance(body, dict) else None
        farm_wide = isinstance(target, dict) and target.get("kind") == "auto"
    if template.startswith(_CONNECTOR_PREFIX):
        await _enforce_connector_scope(db, request, api_key, template, allowed)
    if farm_wide:
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
    return allowed


async def _enforce_connector_scope(
    db: AsyncSession, request: Request, api_key: Any, template: str, allowed: frozenset[int]
) -> None:
    """The connector's own refusals, as codes the plugin reads (``{"error", "message"}``), before any handler."""
    from backend.app.services import connector_submissions as submissions

    printers: set[int] = set()
    auto = False
    if template == _CONNECTOR_SUBMIT:
        body = await _json_body(request)
        entries = body.get("entries") if isinstance(body, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            target = entry.get("target") if isinstance(entry, dict) else None
            if isinstance(target, dict) and target.get("kind") == "auto":
                auto = True
    elif template in _CONNECTOR_OPERATION_ROUTES:
        operation_id = request.path_params.get("operation_id")
        own = await submissions.find_own(db, getattr(api_key, "connector_identity", None), str(operation_id))
        if own is not None:
            printers, auto = submissions.manifest_targets(own.manifest)
    if auto:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "auto_requires_unrestricted_key", "message": submissions.AUTO_RESTRICTED},
        )
    try:
        printers |= await printers_named_by(db, request, template)
    except _Unreadable:
        printers = {-1}
    for printer_id in sorted(printers):
        if printer_id not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "printer_scope", "message": submissions.PRINTER_SCOPE},
            )
