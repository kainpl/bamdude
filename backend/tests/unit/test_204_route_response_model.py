"""Every 204 route in a future-annotations module must declare response_model=None.

Under ``from __future__ import annotations`` a handler's ``-> None`` return
annotation reaches FastAPI as the *string* ``"None"``. FastAPI resolves that to
``NoneType`` — a truthy class — and then asserts that a 204 response may not
carry a body, so the app fails at **import**, not at request time. fastapi
>= 0.116 special-cases ``NoneType``; the 0.109-0.115 releases our requirements
floor still permits do not, so a fresh install that resolves into that window
will not boot at all.

Upstream hit this on one route (Bambuddy e1e2c12d). We had three. This guard
exists so the fourth cannot be added silently — it is a drift test, not a
behaviour test: it reads the source, because the failure it guards happens
before any test client can be constructed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROUTES_DIR = Path(__file__).resolve().parents[2] / "app" / "api" / "routes"


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(a.name == "annotations" for a in node.names)
        for node in tree.body
    )


def _route_decorators(tree: ast.Module):
    """Yield (function_node, decorator_node) for every @router.<verb>(...) handler."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id.endswith("router"):
                yield node, dec


def _kwarg(dec: ast.Call, name: str):
    for kw in dec.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _collect_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _has_future_annotations(tree):
            continue  # the annotation is a real object here, not a string
        for func, dec in _route_decorators(tree):
            status = _kwarg(dec, "status_code")
            if not (isinstance(status, ast.Constant) and status.value == 204):
                continue
            # Only a `-> None` annotation triggers the assert.
            returns = func.returns
            is_none_return = isinstance(returns, ast.Constant) and returns.value is None
            if not is_none_return:
                continue
            if _kwarg(dec, "response_model") is None:
                offenders.append(f"{path.name}:{func.lineno} {func.name}")
    return offenders


def test_204_routes_declare_response_model():
    offenders = _collect_offenders()
    assert not offenders, (
        "These 204 routes live in a module with `from __future__ import annotations` "
        "and annotate `-> None`, but do not declare `response_model=None`. On "
        "fastapi 0.109-0.115 (inside our requirements floor) the app will not "
        "import at all:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "module",
    ["filament_calibration.py", "library_tags.py"],
)
def test_known_offenders_stay_fixed(module):
    """The three routes this guard was written for, pinned by module."""
    tree = ast.parse((ROUTES_DIR / module).read_text(encoding="utf-8"))
    assert _has_future_annotations(tree), f"{module} lost its future-annotations import"
    found = 0
    for func, dec in _route_decorators(tree):
        status = _kwarg(dec, "status_code")
        if isinstance(status, ast.Constant) and status.value == 204:
            found += 1
            assert _kwarg(dec, "response_model") is not None, f"{module}:{func.lineno} {func.name}"
    assert found, f"{module} has no 204 route any more — update or drop this test"
