"""Literal paths must precede same-prefix dynamic matchers (Starlette order).

``PATCH /spools/bulk-update`` sat below ``PATCH /spools/{spool_id}`` in the
spoolman inventory router, so Starlette matched the dynamic route first, int
validation rejected "bulk-update", and every call 422'd without reaching the
handler — while the sibling in inventory.py documented exactly this trap.
"""

from backend.app.api.routes.spoolman_inventory import router


def _patch_route_index(path_suffix: str) -> int:
    for i, route in enumerate(router.routes):
        if getattr(route, "path", "").endswith(path_suffix) and "PATCH" in (getattr(route, "methods", set()) or set()):
            return i
    raise AssertionError(f"no PATCH route ending with {path_suffix}")


def test_bulk_update_precedes_the_dynamic_spool_matcher():
    assert _patch_route_index("/spools/bulk-update") < _patch_route_index("/spools/{spool_id}")
