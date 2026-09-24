"""The API-key printer scope reads every printer a key-reachable route can be told about.

``core/api_key_scope`` finds the printers a request names by FIELD NAME —
``printer_id`` / ``printer_ids`` / ``queue_id`` / … in the path, the query or
anywhere in a JSON body. That is only as good as the names: a route an API key
can reach that takes ``target_printer_id`` would slip past it without a sound.
So this walks every route a key can reach and demands that each printer- or
queue-looking name is one the scope reads, or is listed here as not a printer
with the reason.

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
    "enqueue_position": "where in the queue a new job goes: 'end' or 'next'",
    "printer_reachable": "a label bridge reporting whether ITS label printer answers",
    "printer_models": "model names a maintenance type applies to, not printers",
    "printer_model": "a model name, not a printer",
    "printer_name": "a preset's printer-model name, not a printer",
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


def test_the_walk_sees_the_routes_we_know():
    seen = {(method, r.path) for r in _key_routes() for method in r.methods}

    assert {
        ("POST", "/api/v1/queue/"),
        ("GET", "/api/v1/printers/{printer_id}/status"),
        ("POST", "/api/v1/printers/{printer_id}/print/stop"),
        ("GET", "/api/v1/queue/"),
    } <= seen


def test_every_printer_a_key_can_name_is_read_by_the_scope():
    unread = sorted(
        {
            (route.path, name)
            for route in _key_routes()
            if not _farm_wide(route.path)
            for name in _names(route)
            if _LOOKS_LIKE_A_PRINTER.search(name) and name not in READ_BY_THE_SCOPE and name not in NOT_A_PRINTER
        }
    )

    assert not unread, (
        "A route an API key can reach takes a printer under a name core/api_key_scope does not read. "
        "Read it there, or add it to NOT_A_PRINTER with the reason: " + repr(unread)
    )


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
        for prefix, param, _resolve in api_key_scope._PATH_RESOLVERS
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
