"""The registry of shapes that carry a part's picture and the routes that answer with them (spec §11.3; plan E4)."""

import importlib
import inspect
import pkgutil
import typing

from fastapi.routing import APIRoute
from pydantic import BaseModel

import backend.app.schemas as schemas
from backend.app.services import part_images

PART_FIELDS = {"part_id", "product_part_id", "source_part_id", "target_part_id"}

#: Shapes that name a part and deliberately carry no picture -- each with its reason.
NOT_A_SURFACE = {
    "archive.ArchivePartRow": "a print's part row (print_archive_parts), not a product part (spec §11.3)",
    "printer.AirductFan": "a printer fan's part number, not a product part",
    "product.ProductPartMerge": "a request body",
    "product.StockAdjustIn": "a request body",
    "project.FulfilmentPartIn": "a request body",
    "product.EstimateSurplusOut": "the product estimate's surplus figures -- no surface of spec §12.2",
    "product.PartSourcesOut": "a part's sources, shown under the part's own row, which carries the picture",
    "project.LineChangedPartOut": "a configuration caption; the variant chips stay as they are (spec §12.2)",
    "project.ProcurementOut": "the order's procurement figures -- no surface of spec §12.2",
    "project.StockMovedOut": "the answer to «bank surplus», told as a toast (spec §12.2: toasts unchanged)",
}
#: The picture's own vocabulary names plate objects by identify_id -- it is not a surface.
OWN_MODULE = "part_image"


def _candidates():
    for module in pkgutil.iter_modules(schemas.__path__):
        if module.name == OWN_MODULE:
            continue
        loaded = importlib.import_module(f"backend.app.schemas.{module.name}")
        for name, cls in vars(loaded).items():
            if not (inspect.isclass(cls) and issubclass(cls, BaseModel) and cls.__module__ == loaded.__name__):
                continue
            fields = set(cls.model_fields)
            if (
                fields & PART_FIELDS
                or {"id", "name_key"} <= fields
                or "identify_id" in fields
                or {"name_key", "count"} <= fields
            ):
                yield f"{module.name}.{name}", cls


def _registered(cls) -> bool:
    shapes = part_images.part_image_shapes()
    return any(c in shapes for c in cls.__mro__)


def test_every_shape_that_names_a_part_is_registered_or_explained():
    undecided = [name for name, cls in _candidates() if not _registered(cls) and name not in NOT_A_SURFACE]
    assert undecided == []


def test_the_allowlist_holds_no_stale_names():
    assert set(NOT_A_SURFACE) <= {name for name, _cls in _candidates()}


def test_every_registered_shape_carries_a_picture_where_it_names_the_part():
    for cls, kind in part_images.part_image_shapes().items():
        if kind == "plate":
            assert {"library_file_id", "plate_index"} <= set(cls.model_fields)
            continue
        assert "image" in cls.model_fields, cls.__name__
        assert {"id": "id", "part_id": "part_id", "instance": "identify_id"}[kind] in cls.model_fields, cls.__name__


def _models(tp, seen: set) -> set:
    found = set()
    if typing.get_origin(tp) is not None:
        for arg in typing.get_args(tp):
            found |= _models(arg, seen)
        return found
    if isinstance(tp, type) and issubclass(tp, BaseModel) and tp not in seen:
        seen.add(tp)
        found.add(tp)
        for field in tp.model_fields.values():
            found |= _models(field.annotation, seen)
    return found


def _picture_routes():
    from backend.app.main import app

    for route in app.routes:
        if isinstance(route, APIRoute) and route.response_model is not None:
            if any(_registered(m) for m in _models(route.response_model, set())):
                yield route


def test_every_route_that_answers_a_part_fills_its_picture():
    missing = [
        f"{sorted(r.methods)[0]} {r.path}"
        for r in _picture_routes()
        if not getattr(r.endpoint, "__part_images__", None)
    ]
    assert missing == []


def test_the_route_scan_sees_the_routes_it_guards():
    found = {r.path for r in _picture_routes()}
    assert {
        "/api/v1/products/{product_id}",
        "/api/v1/products/{product_id}/plates",
        "/api/v1/stock/journal",
        "/api/v1/stock/movements",
        "/api/v1/projects/{project_id}/fulfilment",
    } <= found
    # (method, path) pairs: several methods share a path, so paths alone undercount (51 routes, 2026-10-10)
    assert len({(sorted(r.methods)[0], r.path) for r in _picture_routes()}) >= 51


def test_a_filling_route_has_its_session_by_name():
    for route in _picture_routes():
        assert "db" in inspect.signature(route.endpoint).parameters, route.path


def test_the_editor_routes_are_the_editors():
    from backend.app.main import app

    editors = {
        r.path
        for r in app.routes
        if isinstance(r, APIRoute) and getattr(r.endpoint, "__part_images__", None) == "editor"
    }
    assert "/api/v1/products/{product_id}" in editors
    assert "/api/v1/products/{product_id}/parts/{part_id}" in editors
    assert "/api/v1/projects/{project_id}" not in editors
