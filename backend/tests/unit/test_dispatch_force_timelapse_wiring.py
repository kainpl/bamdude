"""Regression test for the wiring of the #1397 force-timelapse fix.

Ported from upstream Bambuddy's ``test_scheduler_force_timelapse_wiring.py``
but re-targeted at BamDude's architecture. Upstream wired the override into
TWO places — ``background_dispatch.py`` (Print Now / Reprint) AND
``print_scheduler.py:_start_print`` (the queue) — because its scheduler calls
``printer_manager.start_print`` directly, and the first attempt forgot that
call site (field-test caught queued prints slipping through).

BamDude's single-dispatch-layer invariant means ``print_scheduler`` does NOT
call ``start_print`` — it hands an options dict to
``background_dispatch._process_job``, so the queue path funnels through the
SAME two ``printer_manager.start_print`` sites as Print Now / Reprint. Those
two sites are the only ``start_print`` callers in the backend, so the resolver
belongs there and nowhere else. This test pins that: BOTH sites must pass the
resolved ``effective_timelapse`` (not the raw ``job.options`` value), and the
module must define the shared resolver. If a refactor drops the resolver at
either site, queued/direct prints lose their finish-photo source and this
fails.
"""

import ast
from pathlib import Path

DISPATCH_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "services" / "background_dispatch.py"


def _find_calls_to_start_print(tree: ast.AST) -> list[ast.Call]:
    """Return every ``printer_manager.start_print(...)`` Call node. BamDude has
    exactly two — the only start_print callers in the backend."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "start_print":
            continue
        value = func.value
        if isinstance(value, ast.Name) and value.id == "printer_manager":
            calls.append(node)
    return calls


def test_both_start_print_sites_use_resolved_timelapse():
    """Every ``printer_manager.start_print(timelapse=...)`` in background_dispatch
    must pass ``effective_timelapse`` (the resolved value) — not
    ``job.options.get("timelapse", ...)`` (the user's raw choice). A refactor
    that restores the raw value at either site would silently drop the #1397
    override for that dispatch path."""
    tree = ast.parse(DISPATCH_PATH.read_text())
    calls = _find_calls_to_start_print(tree)

    assert len(calls) == 2, (
        f"expected exactly two printer_manager.start_print(...) sites in "
        f"background_dispatch.py (the only start_print callers in the backend), "
        f"found {len(calls)} — if a third dispatch path was added it must also "
        f"resolve effective_timelapse for #1397."
    )

    for call in calls:
        timelapse_kw = next((kw for kw in call.keywords if kw.arg == "timelapse"), None)
        assert timelapse_kw is not None, "start_print(timelapse=...) kwarg is missing at a dispatch site"
        value = timelapse_kw.value
        assert isinstance(value, ast.Name) and value.id == "effective_timelapse", (
            f"timelapse= must be the resolver's return value (effective_timelapse), "
            f"got {ast.dump(value)}. Both dispatch sites must apply the #1397 "
            f"override or that path's finish-photo extractor has nothing to pull from."
        )


def test_module_defines_resolve_effective_timelapse():
    """The shared resolver must live in background_dispatch (module-level) so
    both dispatch sites — and the print-queue path that funnels through them —
    apply the same override. Guards against a refactor removing it."""
    source = DISPATCH_PATH.read_text()
    assert "async def resolve_effective_timelapse(" in source, (
        "background_dispatch.py must define the module-level resolve_effective_timelapse for #1397"
    )
