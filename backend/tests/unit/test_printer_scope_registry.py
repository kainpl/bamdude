"""N62 §9: every API route is classified against the printer scope (spec r2, r1 B05).

A route EITHER names a printer the permission gate reads (a path parameter, or a REQUIRED query /
body field the scanner of ``core/api_key_scope`` reads, or a path id it resolves to its printer),
OR reads the request's scope itself (``core/printer_scope.request_printer_scope`` and its kin —
in the handler, in a dependency, or in a helper of its module), OR is listed below as not about a
printer — by name, or as a whole module — with the reason.

An OPTIONAL printer filter names nothing when it is left out: a list behind one has to read the
scope (that is how ``GET /spoolman/inventory/slot-assignments/all`` was found to list every
printer's slots). "Reads the scope" proves the scope is read, not that every list applies it —
the area tests of ``test_printer_scope_api`` carry that.

The tripwire: a new route — or a route of a listed module that gains a printer — fails here until
it is classified.
"""

from __future__ import annotations

import inspect
import sys

from fastapi.routing import APIRoute

from backend.app.core import api_key_scope
from backend.app.main import app

_TOKENS = (
    "request_printer_scope",
    "in_request_scope",
    "sensor_in_request_scope",
    "target_in_request_scope",
    "request_key_listed",
    "printer_scope",
)
_READ = (
    api_key_scope._PRINTER_KEYS
    | api_key_scope._PRINTER_CSV_QUERY
    | api_key_scope._QUEUE_ITEM_KEYS
    | api_key_scope._ARCHIVE_KEYS
    | api_key_scope._QUEUE_ROUTER_ITEM_KEYS
    | {"items"}
)

# Whole modules that hold no row of a printer — or whose printer-bound readers read the scope
# themselves (and are then classified by that, not by this list).
MODULES: dict[str, str] = {
    "api_keys": "keys are their owner's; a key's own printer list is written here and read only by core/printer_scope",
    "auth": "sign-in, tokens and the caller's own account; the tokens a door takes carry their holder's scope (§5.1)",
    "mfa": "the caller's own second factor",
    "users": "accounts; an account's printers come from its groups",
    "groups": "group administration — the printer access of a group is written here (§6)",
    "bug_report": "an administrator's support bundle",
    "cloud": "the Bambu Cloud account of the installation",
    "cloud_link": "the BamDude Cloud link of the installation",
    "connector": "the slicer connector is key-only; api_key_scope._enforce_connector_scope asks key_scope of every entry target",
    "credential_rollout": "the installation's credential rollout",
    "customers": "Workshop customers — not a printer's",
    "delivery_methods": "Workshop delivery methods — not a printer's",
    "discovery": "an administrator's network discovery of printers not added yet",
    "external_links": "sidebar links",
    "filament_families": "filament presets and the printer MODELS they apply to",
    "git_backup": "the installation's Git export",
    "hms": "the HMS error catalogue",
    "inbox": "the caller's own inbox; delivery is narrowed by notification_inbox.recipients (§5.5)",
    "label_devices": "label printers — not farm printers",
    "label_templates": "label designs",
    "labels": "label printing",
    "library": "the library is not a printer's (§11)",
    "library_assembly": "the library is not a printer's (§11)",
    "library_notes": "the library is not a printer's (§11)",
    "library_photos": "the library is not a printer's (§11)",
    "library_tags": "the library is not a printer's (§11)",
    "library_thumbnail_jobs": "the library is not a printer's (§11)",
    "library_trash": "the library is not a printer's (§11)",
    "local_backup": "the installation's backups",
    "local_presets": "slicer presets",
    "macros": "G-code macro definitions per printer MODEL, not per printer",
    "makerworld": "MakerWorld models",
    "mcp_integration": "an administrator's MCP set-up; the MCP door takes key_scope (mcp_server/auth.py)",
    "metrics": "Prometheus, behind the installation's metrics token",
    "model_sources": "3D-model sites",
    "notification_templates": "message templates",
    "orca_cloud": "the Orca Cloud account",
    "overlay_branding": "the installation's overlay logo",
    "part_images": "product part pictures — Workshop, not a printer's",
    "print_options_preferences": "the caller's print-dialog preferences",
    "printer_tags": "the tag catalogue printers are labelled with",
    "product_categories": "Workshop catalogue",
    "products": "Workshop catalogue — not a printer's",
    "projects": "orders are facts of the order, not of a printer (§5.2); their printer-bound lists read the scope",
    "settings": "the installation's settings",
    "slice_jobs": "slicing jobs of library files",
    "slicer_pipelines": "slicer pipelines",
    "slicer_presets": "slicer presets",
    "stock": "finished goods — not a printer's",
    "stock_issues": "stock issues — not a printer's",
    "support": "an administrator's logs",
    "system": "the installation's health",
    "telegram": "chat configuration; a chat is its own authority, narrowed by its group (D4)",
    "updates": "BamDude updates",
    "user_notifications": "the caller's own e-mail preferences",
    "virtual_printers": "Virtual Printers — administrative (§5.2)",
    "cameras": "standalone cameras are not printers (inv-standalone-cameras-are-not-printers)",
    "camwall": "the wall's standalone cameras; its printer list reads the token owner's scope",
    "auto_queue": "AutoQueue rows are seen by queue rights (§12.3); routing holds a job to its author's printers (D2)",
    "inventory": "spools, catalogues and storage are not a printer's (§11); slot lists, usage and weight sync read the scope",
    "spoolman": "the Spoolman connection and its spools; the AMS sync of every printer reads the scope",
    "spoolman_inventory": "Spoolman spools are not a printer's (§11); slot lists and weight sync read the scope",
    "main": "the frontend shell and the health probe",
}

# Routes of mixed modules that are not about a printer.
ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/archives/trash/settings"): "the trash retention setting",
    ("PUT", "/api/v1/archives/trash/settings"): "the trash retention setting",
    ("GET", "/api/v1/archives/cleanup/status"): "the 3MF clean-up loop's status (archives:read)",
    ("GET", "/api/v1/archives/cleanup/preview"): "a count of the clean-up's candidates, no rows",
    ("POST", "/api/v1/archives/cleanup/run"): "administrative clean-up (archives:delete_all)",
    ("POST", "/api/v1/archives/search/rebuild-index"): "administrative index rebuild (archives:update_all)",
    ("POST", "/api/v1/archives/backfill-hashes"): "administrative back-fill (archives:update_all)",
    (
        "POST",
        "/api/v1/printers/camera/stream-token",
    ): "mints a token naming its holder; every stream asks that holder's scope",
    ("GET", "/api/v1/ha-sensors/entities"): "Home Assistant's entity list, not a binding",
    ("POST", "/api/v1/octoeverywhere/test-connection"): "an administrator's connection test",
    ("DELETE", "/api/v1/obico/history"): "administrative (settings:update) clearing of the detection history",
    ("POST", "/api/v1/obico/test-connection"): "an administrator's connection test",
    ("GET", "/api/v1/obico/cached-frame/{nonce}"): "a single-use nonce handed to Obico's server, no person",
    ("POST", "/api/v1/firmware/store/download"): "the firmware store, no printer",
    ("POST", "/api/v1/firmware/batch"): "names every target printer in targets[]",
    ("POST", "/api/v1/firmware/batch/preview"): "names every target printer in targets[]",
    ("GET", "/api/v1/maintenance/types"): "maintenance types, per model",
    ("POST", "/api/v1/maintenance/types"): "maintenance types, per model",
    ("PATCH", "/api/v1/maintenance/types/{type_id}"): "maintenance types, per model",
    ("DELETE", "/api/v1/maintenance/types/{type_id}"): "maintenance types, per model",
    ("POST", "/api/v1/maintenance/types/restore-defaults"): "maintenance types, per model",
    ("GET", "/api/v1/notifications/"): "notification providers — an administrator's configuration",
    ("POST", "/api/v1/notifications/"): "notification providers — an administrator's configuration",
    ("POST", "/api/v1/notifications/test-config"): "a provider test",
    ("POST", "/api/v1/notifications/test-all"): "a provider test",
    (
        "POST",
        "/api/v1/notifications/external-message",
    ): "an external app's message; its optional printer is read by the gate",
    ("GET", "/api/v1/notifications/photos/{filename}"): "a notification's photo copy, by its unguessable name",
    ("GET", "/api/v1/notifications/events"): "the event catalogue",
    ("DELETE", "/api/v1/notifications/logs"): "administrative log retention (notifications:delete)",
    ("GET", "/api/v1/notifications/{provider_id}"): "notification providers — an administrator's configuration",
    ("PATCH", "/api/v1/notifications/{provider_id}"): "notification providers — an administrator's configuration",
    ("DELETE", "/api/v1/notifications/{provider_id}"): "notification providers — an administrator's configuration",
    ("POST", "/api/v1/notifications/{provider_id}/test-config"): "a provider test",
    ("POST", "/api/v1/notifications/{provider_id}/test"): "a provider test",
    ("POST", "/api/v1/queue/next-block"): "names its printer in items[] (the gate reads it)",
    ("POST", "/api/v1/queue/reorder"): "names its jobs in items[] (the gate reads them)",
    ("POST", "/api/v1/printer-locations"): "a new empty location reaches no printer",
    ("GET", "/api/v1/printers/model-compatibility"): "the model compatibility matrix",
    ("POST", "/api/v1/printers/diagnostic"): "a connection test of an address, no printer row",
    ("POST", "/api/v1/smart-plugs/"): "a new plug; its optional printer is read by the gate",
    ("POST", "/api/v1/smart-plugs/discover/scan"): "network discovery of plugs not added yet",
    ("GET", "/api/v1/smart-plugs/discover/status"): "network discovery of plugs not added yet",
    ("POST", "/api/v1/smart-plugs/discover/stop"): "network discovery of plugs not added yet",
    ("GET", "/api/v1/smart-plugs/discover/devices"): "network discovery of plugs not added yet",
    ("POST", "/api/v1/smart-plugs/ha/test-connection"): "a connection test",
    ("POST", "/api/v1/smart-plugs/rest/test-connection"): "a connection test",
    ("GET", "/api/v1/smart-plugs/ha/entities"): "Home Assistant's entity list, not a plug",
    ("GET", "/api/v1/smart-plugs/ha/sensors"): "Home Assistant's entity list, not a plug",
    ("POST", "/api/v1/smart-plugs/test-connection"): "a connection test",
    ("GET", "/api/v1/spoolman/inventory/usage/source"): "which Spoolman the history belongs to",
    ("POST", "/api/v1/spoolman/inventory/usage/source/keep-previous"): "which Spoolman the history belongs to",
    ("POST", "/api/v1/spoolman/inventory/usage/source/rotate"): "which Spoolman the history belongs to",
    ("DELETE", "/api/v1/spoolman/inventory/spools/{spool_id}/usage"): "a spool's ledger correction (inventory:update)",
    ("DELETE", "/api/v1/spoolman/inventory/spools/{spool_id}/usage/{usage_id}"): "a spool's ledger correction",
    ("POST", "/api/v1/statistics/recalculate-costs"): "administrative (archives:update_all), §5.2",
    ("POST", "/api/v1/statistics/reprice-usage"): "administrative, §5.2",
    ("GET", "/api/v1/zigbee/status"): "the radio coordinator",
    ("GET", "/api/v1/zigbee/ports"): "the radio coordinator",
    ("POST", "/api/v1/zigbee/disconnect"): "the radio coordinator",
    ("DELETE", "/api/v1/zigbee/network"): "the radio coordinator",
    ("POST", "/api/v1/zigbee/permit"): "the radio coordinator",
    ("POST", "/api/v1/zigbee/restart"): "the radio coordinator",
    (
        "GET",
        "/api/v1/zigbee/devices",
    ): "the mesh's devices; adopted sensors and plugs are listed by their own scoped lists",
    ("GET", "/api/v1/zigbee/devices/{ieee}/settings"): "a mesh device's reporting settings",
    ("PUT", "/api/v1/zigbee/devices/{ieee}/settings"): "a mesh device's reporting settings",
    ("DELETE", "/api/v1/zigbee/devices/{ieee}/settings"): "a mesh device's reporting settings",
    ("DELETE", "/api/v1/zigbee/devices/{ieee}"): "removes a device from the mesh",
    ("GET", "/api/v1/zigbee/devices/{ieee}/attributes"): "a mesh device's attributes",
}

_PREFIX = "backend.app.api.routes."


def _module_key(route: APIRoute) -> str:
    module = route.endpoint.__module__
    if module == "backend.app.main":
        return "main"
    return module[len(_PREFIX) :] if module.startswith(_PREFIX) else module


def _required_names(route: APIRoute) -> set[str]:
    out = {p.name for p in route.dependant.path_params}
    out |= {p.name for p in route.dependant.query_params if p.field_info.is_required()}
    for param in route.dependant.body_params:
        if param.field_info.is_required():
            out.add(param.name)
        fields = getattr(param.field_info.annotation, "model_fields", None) or {}
        out |= {name for name, field in fields.items() if field.is_required()}
    return out


def _resolved(route: APIRoute) -> bool:
    params = {p.name for p in route.dependant.path_params}
    return any(
        route.path.startswith(prefix) and param in params for prefix, param, _r, _s in api_key_scope._PATH_RESOLVERS
    )


def _source(call) -> str:
    try:
        return inspect.getsource(call)
    except (OSError, TypeError):
        return ""


_HELPERS: dict[str, set[str]] = {}


def _scope_helpers(module) -> set[str]:
    if module.__name__ not in _HELPERS:
        funcs = {
            name: _source(obj)
            for name, obj in vars(module).items()
            if inspect.isfunction(obj) and obj.__module__ == module.__name__
        }
        found = {name for name, src in funcs.items() if any(t in src for t in _TOKENS)}
        for _ in range(3):
            found |= {name for name, src in funcs.items() if any(f"{h}(" in src for h in found)}
        _HELPERS[module.__name__] = found
    return _HELPERS[module.__name__]


def _reads_scope(route: APIRoute) -> bool:
    helpers = _scope_helpers(sys.modules[route.endpoint.__module__])
    sources = [_source(route.endpoint)]
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            sources.append(_source(dep.call))
        stack.extend(dep.dependencies)
    return any(any(t in src for t in _TOKENS) or any(f"{h}(" in src for h in helpers) for src in sources)


def _routes() -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]


def test_every_route_is_classified():
    unclassified = sorted(
        f"{method} {route.path} ({route.endpoint.__module__}.{route.endpoint.__name__})"
        for route in _routes()
        for method in route.methods
        if not (
            _required_names(route) & _READ
            or _resolved(route)
            or _reads_scope(route)
            or _module_key(route) in MODULES
            or (method, route.path) in ROUTES
        )
    )
    assert not unclassified, (
        "A route neither names a printer the gate reads nor reads the request's printer scope "
        "(core/printer_scope.request_printer_scope). Narrow it, or list it in MODULES / ROUTES "
        "with the reason: \n  " + "\n  ".join(unclassified)
    )


def test_every_listed_route_still_exists():
    present = {(method, route.path) for route in _routes() for method in route.methods}
    assert not sorted(set(ROUTES) - present), sorted(set(ROUTES) - present)
    modules = {_module_key(route) for route in _routes()}
    assert not sorted(set(MODULES) - modules), sorted(set(MODULES) - modules)


def test_the_walk_sees_the_readers_we_know():
    """A refactor that hid the scope from the walk would let everything through as "reads it"."""
    by_path = {(method, r.path): r for r in _routes() for method in r.methods}
    for key in (
        ("GET", "/api/v1/archives/"),
        ("GET", "/api/v1/statistics/overview"),
        ("GET", "/api/v1/spoolman/inventory/slot-assignments/all"),
        ("GET", "/api/v1/maintenance/overview"),
        ("GET", "/api/v1/smart-plugs/"),
    ):
        assert _reads_scope(by_path[key]), key
    assert not _reads_scope(by_path[("GET", "/api/v1/printers/model-compatibility")])
