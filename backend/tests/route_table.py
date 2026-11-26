"""The paths and methods the OpenAPI schema would list, read off the route table it is built from.

``app.openapi()`` generates the whole schema (6-10 s on a CI worker, cached per process), and a
test that only asks whether a path is registered paid that on every xdist worker it landed on.
FastAPI's schema takes its paths from exactly these routes: ``APIRoute``s with
``include_in_schema``, keyed by ``path_format``. So "absent from the schema" keeps its meaning.
"""

from fastapi import FastAPI
from fastapi.routing import APIRoute


def schema_paths(app: FastAPI) -> dict[str, set[str]]:
    """``{path: {lower-case methods}}`` for every route the OpenAPI schema includes."""
    paths: dict[str, set[str]] = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.include_in_schema:
            paths.setdefault(route.path_format, set()).update(method.lower() for method in route.methods)
    return paths
