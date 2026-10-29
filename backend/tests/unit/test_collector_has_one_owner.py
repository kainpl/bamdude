"""The cyclic collector is the process's, and only ``core/process_gc.py`` may switch it.

Codex r1 T-R4 (2026-10-28): a pause around the rebalance decision moved a full collection from the
worker onto the loop and never made one rarer; switching the collector is a policy of the whole
process. The agreed policy (spec process-gc-latency r2, 2026-10-29) has ONE owner, called once
by the lifespan; nothing else may disable, enable, freeze, unfreeze, retune or run it — a manual
full collection while serving requests is exactly the pause the policy exists to remove. Reading
it (``get_objects``, ``get_freeze_count``, the support census) is fine.

An AST scan rather than a regex: ``import gc as g``, ``from gc import freeze`` and handing the
module on (``collector=gc``, then ``self._gc.freeze()``) all slipped past the regex this replaces.
"""

import ast
from pathlib import Path

import pytest

SWITCHES = frozenset({"disable", "enable", "freeze", "unfreeze", "set_threshold", "collect"})
ROOT = Path(__file__).resolve().parents[2] / "app"
OWNER = "core/process_gc.py"


def switches_collector(source: str) -> bool:
    """Whether ``source`` switches the collector, or hands the module to something that may."""
    tree = ast.parse(source)
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "gc")
        elif isinstance(node, ast.ImportFrom) and node.module == "gc":
            if any(alias.name in SWITCHES or alias.name == "*" for alias in node.names):
                return True
        elif isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if called in ("import_module", "__import__") and isinstance(first, ast.Constant) and first.value == "gc":
                return True
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "gc":
            return True
    if not aliases:
        return False
    attribute_bases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            attribute_bases.add(id(node.value))
            if node.attr in SWITCHES:
                return True
    # The module itself handed on — a default argument, an assignment, a call argument.
    return any(
        isinstance(node, ast.Name)
        and node.id in aliases
        and isinstance(node.ctx, ast.Load)
        and id(node) not in attribute_bases
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize(
    "source",
    [
        "import gc\ngc.disable()",
        "import gc\ngc.freeze()",
        "import gc\ngc.collect()",
        "import gc\ngc.set_threshold(1, 2, 3)",
        "import gc as g\ng.unfreeze()",
        "from gc import freeze",
        "from gc import *",
        "import gc\ncollector = gc\ncollector.freeze()",
        "import gc\ndef f(collector=gc):\n    collector.enable()",
        "import gc\nrun(gc)",
        "import importlib\nimportlib.import_module('gc').freeze()",
        "import sys\nsys.modules['gc'].disable()",
        "def f():\n    import gc\n    gc.collect(2)",
    ],
)
def test_the_scan_sees_every_way_to_switch_it(source):
    assert switches_collector(source)


@pytest.mark.parametrize(
    "source",
    [
        "import gc\nfor obj in gc.get_objects():\n    pass",
        "import gc\nn = gc.get_freeze_count()",
        "from gc import get_objects",
        "from math import gcd",
        "gc = 3\ngc.real",
        "x = 'gcode'",
    ],
)
def test_reading_the_collector_is_not_switching_it(source):
    assert not switches_collector(source)


def test_only_the_policy_module_switches_the_collector():
    offenders = sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*.py")
        if switches_collector(path.read_text(encoding="utf-8"))
    )
    assert offenders == [OWNER]
