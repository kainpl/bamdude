"""The printer scope reads every printer a gated route can be told about (N62 §9.2).

``core/api_key_scope`` finds the printers a request names by FIELD NAME —
``printer_id`` / ``printer_ids`` / ``queue_id`` / … in the path, the query or
anywhere in a JSON body. That is only as good as the names: a route an API key
can reach that takes ``target_printer_id`` would slip past it without a sound.
So this walks every route behind a permission gate — a person passes the same
scanner since N62 (``enforce_person_scope``), not only a key — and demands that
each printer- or queue-looking name is one the scope reads, or is listed here as
not a printer with the reason. A printer in a FORM field is refused outright: the
scanner reads JSON only (spec §5.1).

⚠️ Reachability is read off the gate closures (``perm_strings`` / ``all_perm``).
If a refactor of the gates changes those names this walk finds nothing and
would pass vacuously — ``test_the_walk_sees_the_routes_we_know`` fails first.
"""

from __future__ import annotations

import re
import typing

from fastapi.routing import APIRoute
from pydantic import BaseModel

from backend.app.core import api_key_scope
from backend.app.core.auth import _resolve_apikey_scope
from backend.app.main import app

_LOOKS_LIKE_A_PRINTER = re.compile(r"printer|queue", re.IGNORECASE)

# Names a key-reachable route carries that are NOT a printer reference.
NOT_A_PRINTER = {
    "acknowledge_queue_change": "a boolean acknowledgement of reviewed Auto Queue removal, not a printer reference",
    "include_paused_queues": "an order's boolean planning preference, not a printer or queue reference",
    "enqueue_position": "where in the queue a new job goes: 'end' or 'next'",
    "printer_reachable": "a label bridge reporting whether ITS label printer answers",
    "printer_models": "model names a maintenance type applies to, not printers",
    "printer_model": "a model name, not a printer",
    "printer_name": "a preset's printer-model name, not a printer",
    "printer_preset": "a slicer preset reference (source + id) of an assembly target, not a farm printer",
    "controls_printer_power": "a boolean describing the plug's power role, not a printer reference",
    "show_on_printer_card": "a boolean display preference, not a printer reference",
    # N62: names a person's gated routes carry that are not a printer reference.
    "can_control_printer": "an API key's permission flag",
    "can_queue": "an API key's permission flag",
    "restrict_printers": "a group's flag; its printers are written by an administrator (§6)",
    "editing_queue_item": "a boolean: the preview is of an edit (the job itself is source_queue_item_id)",
    "printer_names": "printer PRESET names a filament family applies to",
    "printer_data": "the body of a printer's own create / update route",
    "src_printer_model": "a printer model",
    "dst_printer_model": "a printer model",
    "queue_force_color_match": "a Virtual Printer's boolean",
    "clear_target_printer": "a Virtual Printer's boolean",
    "target_printer_id": "a Virtual Printer's target — Virtual Printers are administrative (§5.2)",
    "printer_location_id": "a printer LOCATION; the sensor routes ask target_in_request_scope (§12.2)",
    # The installation's settings (settings:update), not a person's printer request.
    "default_printer_id": "an installation setting",
    "obico_enabled_printers": "an installation setting",
    "octoeverywhere_enabled_printers": "an installation setting",
    "check_printer_firmware": "an installation setting",
    "per_printer_mapping_expanded": "an installation setting",
    "printer_sensor_history_retention_days": "an installation setting",
    "auto_queue_compatible_models": "an installation setting",
    "auto_queue_rebalance_models": "an installation setting",
    "auto_queue_return_failed": "an installation setting",
    "queue_drying_block": "an installation setting",
    "queue_drying_enabled": "an installation setting",
    "queue_shortest_first": "an installation setting",
    "virtual_printer_access_code": "an installation setting",
    "virtual_printer_archive_name_source": "an installation setting",
    "virtual_printer_enabled": "an installation setting",
    "virtual_printer_mode": "an installation setting",
    # Notification provider event switches.
    "on_printer_error": "a provider's event switch",
    "on_printer_offline": "a provider's event switch",
    "on_printer_queue_completed": "a provider's event switch",
    "on_queue_completed": "a provider's event switch",
    "on_queue_job_added": "a provider's event switch",
    "on_queue_job_failed": "a provider's event switch",
    "on_queue_job_skipped": "a provider's event switch",
    "on_queue_job_started": "a provider's event switch",
    "on_queue_job_waiting": "a provider's event switch",
}

READ_BY_THE_SCOPE = api_key_scope._PRINTER_KEYS | api_key_scope._PRINTER_CSV_QUERY | api_key_scope._QUEUE_ITEM_KEYS


def _gates(dependant) -> typing.Iterator[tuple[str, list[str]]]:
    for dep in dependant.dependencies:
        call = dep.call
        names = getattr(getattr(call, "__code__", None), "co_freevars", ())
        cells = dict(zip(names, getattr(call, "__closure__", None) or (), strict=False))
        if "perm_strings" in cells:
            mode = "any" if "require_any_permission" in call.__qualname__ else "all"
            yield mode, list(cells["perm_strings"].cell_contents)
        elif "all_perm" in cells:
            yield "all", [cells["all_perm"].cell_contents]
        yield from _gates(dep)


def _key_reachable(route: APIRoute) -> bool:
    for mode, perms in _gates(route.dependant):
        scopes = [_resolve_apikey_scope(p) for p in perms]
        if any(scopes) if mode == "any" else (scopes and all(scopes)):
            return True
    return False


def _models(annotation, seen: set) -> typing.Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return
        seen.add(annotation)
        yield annotation
        for field in annotation.model_fields.values():
            yield from _models(field.annotation, seen)
    for arg in typing.get_args(annotation):
        yield from _models(arg, seen)


def _names(route: APIRoute) -> typing.Iterator[str]:
    for param in route.dependant.path_params + route.dependant.query_params:
        yield param.name
    for param in route.dependant.body_params:
        yield param.name
        for model in _models(param.field_info.annotation, set()):
            yield from model.model_fields


def _farm_wide(path: str) -> bool:
    return any(path == r or path.startswith(r + "/") for r in api_key_scope._FARM_WIDE_ROUTERS)


def _key_routes() -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute) and _key_reachable(r)]


def _gated_routes() -> list[APIRoute]:
    """Every route behind a permission gate: a key's, and since N62 a person's too."""
    return [r for r in app.routes if isinstance(r, APIRoute) and any(True for _ in _gates(r.dependant))]


def test_the_walk_sees_the_routes_we_know():
    seen = {(method, r.path) for r in _key_routes() for method in r.methods}

    assert {
        ("POST", "/api/v1/queue/"),
        ("GET", "/api/v1/printers/{printer_id}/status"),
        ("POST", "/api/v1/printers/{printer_id}/print/stop"),
        ("GET", "/api/v1/queue/"),
        # The camera's header door (addendum C4): an API key reaches it, so its printer must be read.
        ("GET", "/api/v1/printers/{printer_id}/camera/stream"),
        ("GET", "/api/v1/printers/{printer_id}/camera/snapshot"),
        ("POST", "/api/v1/connector/submissions"),
        ("POST", "/api/v1/connector/files"),
    } <= seen


def test_every_printer_a_caller_can_name_is_read_by_the_scope():
    """N62 §9.2: every gated route — a person passes the scanner as a key does; the Auto Queue
    is open to a person (D2), so its routes are walked too."""
    unread = sorted(
        {
            (route.path, name)
            for route in _gated_routes()
            for name in _names(route)
            if _LOOKS_LIKE_A_PRINTER.search(name) and name not in READ_BY_THE_SCOPE and name not in NOT_A_PRINTER
        }
    )

    assert not unread, (
        "A gated route takes a printer under a name core/api_key_scope does not read. "
        "Read it there, or add it to NOT_A_PRINTER with the reason: " + repr(unread)
    )


def test_no_gated_route_takes_a_printer_in_a_form_field():
    """The scanner reads the path, the query and a JSON body — never a form (spec §5.1)."""
    from fastapi import params

    in_forms = sorted(
        (route.path, param.name)
        for route in _gated_routes()
        for param in route.dependant.body_params
        if isinstance(param.field_info, params.Form)
        and _LOOKS_LIKE_A_PRINTER.search(param.name)
        and param.name not in NOT_A_PRINTER
    )
    assert not in_forms, in_forms


def test_the_gated_walk_sees_more_than_the_key_walk():
    assert len(_gated_routes()) > len(_key_routes())


def test_every_job_list_on_the_queue_router_is_read_by_the_scope():
    """On ``/queue/…`` a list of jobs is a list of printers once removed."""
    unread = sorted(
        {
            (route.path, name)
            for route in _key_routes()
            if route.path.startswith(api_key_scope._QUEUE_ROUTER)
            for name in _names(route)
            if (name.endswith("item_ids") or name == "items")
            and name not in READ_BY_THE_SCOPE | api_key_scope._QUEUE_ROUTER_ITEM_KEYS | {"items"}
        }
    )

    assert not unread, repr(unread)


def test_every_path_resolver_still_meets_a_route():
    """A renamed route or parameter would switch a resolver off without a sound."""
    templates = [(r.path, {p.name for p in r.dependant.path_params}) for r in app.routes if isinstance(r, APIRoute)]
    idle = [
        (prefix, param)
        for prefix, param, _resolve, _source in api_key_scope._PATH_RESOLVERS
        if not any(path.startswith(prefix) and param in params for path, params in templates)
    ]

    assert not idle, idle


def test_the_farm_wide_router_exists():
    assert any(_farm_wide(r.path) for r in app.routes if isinstance(r, APIRoute))


def test_a_dispatch_job_answers_for_its_printer(monkeypatch):
    """The resolver behind ``DELETE /background-dispatch/{job_id}``: queued, active, gone."""
    from collections import deque

    from backend.app.services.background_dispatch import (
        ActiveDispatchState,
        PrintDispatchJob,
        background_dispatch,
    )

    def job(job_id: int, printer_id: int) -> PrintDispatchJob:
        return PrintDispatchJob(
            id=job_id,
            kind="reprint_archive",
            source_id=None,
            source_name="part.3mf",
            printer_id=printer_id,
            printer_name=f"P{printer_id}",
        )

    monkeypatch.setattr(background_dispatch, "_queued_jobs", deque([job(1, 10)]))
    monkeypatch.setattr(background_dispatch, "_active_jobs", {2: ActiveDispatchState(job=job(2, 20), message="")})

    assert [background_dispatch.printer_of_job(i) for i in (1, 2, 3)] == [10, 20, None]
