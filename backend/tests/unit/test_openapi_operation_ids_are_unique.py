"""No two operations in the published schema share an operationId.

FastAPI derives the id once per ROUTE, not per operation::

    operation_id = f"{route.name}{route.path_format}"
    operation_id = f"{operation_id}_{list(route.methods)[0].lower()}"

So a route declared with ``api_route(methods=["GET", "POST"])`` hands the SAME
id to both of its operations. That is a duplicate in the schema — and an
unstable one, because the method that names it is read out of a ``set``, whose
order follows the process's hash seed: ``/favicon.ico`` answered to
``serve_favicon_favicon_ico_get`` under ``PYTHONHASHSEED=1`` and to
``serve_favicon_favicon_ico_head`` under ``PYTHONHASHSEED=3`` (measured
2026-09-20). A generated client is therefore not reproducible, and two clients
generated from the same commit can disagree about what a method is called.

Both assertions below describe the same rule from its two ends: the schema must
not repeat an id, and — the cause, caught where the author can see it — no
route may carry more than one method. Declare one decorator per method instead.
"""

from collections import defaultdict

import pytest
from fastapi.routing import APIRoute

from backend.app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def schema() -> dict:
    return app.openapi()


def test_no_two_operations_share_an_operation_id(schema: dict) -> None:
    where: dict[str, list[str]] = defaultdict(list)
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if isinstance(operation, dict) and "operationId" in operation:
                where[operation["operationId"]].append(f"{method.upper()} {path}")

    duplicates = {op_id: paths for op_id, paths in where.items() if len(paths) > 1}
    assert not duplicates, "operationId used more than once:\n" + "\n".join(
        f"  {op_id}\n" + "\n".join(f"      {p}" for p in paths) for op_id, paths in sorted(duplicates.items())
    )


def test_every_route_carries_exactly_one_method() -> None:
    """The declaration-site half of the rule above.

    A route with two methods cannot have two ids, so this is what makes the
    schema check unfailable rather than merely currently-passing.
    """
    offenders = [
        f"  {sorted(route.methods)} {route.path}  ({route.name})"
        for route in app.routes
        if isinstance(route, APIRoute) and len(route.methods) != 1
    ]
    assert not offenders, (
        "these routes answer several methods from one declaration, so FastAPI gives "
        "their operations one shared id — declare a decorator per method:\n" + "\n".join(offenders)
    )
