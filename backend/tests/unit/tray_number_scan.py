"""The scanner behind ``test_tray_numbers_have_one_home.py``: hand-written tray arithmetic outside ``utils/tray_ids``.

A finding is a ``(file, function, form)``: an expression that names a tray-ish
value — an identifier matching ``ams|tray|unit|slot|global`` — in one of the
forms that compute, split, compare or label a tray number:

* direct: ``x * 4 + y``, ``x << 8 | y``, ``254 + x`` / ``x + 254``;
* inverse: ``x // 4``, ``x % 4``, ``x & 3``, ``x >> 2``, ``x >> 8``,
  ``x & 0xFF``, ``x - 254``, ``x - 128``;
* comparisons: ``x >= 254``, ``x < 254``, ``x >= 128``, ``128 <= x <= 135``;
* labels: ``chr(65 + ...)``.

It is a lexical rule on purpose: it cannot tell a tray from a temperature, so
what it finds is either moved to the helper or named, with its stage and
reason, in the test's exemption list.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

TRAYISH = re.compile(r"ams|tray|unit|slot|global", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    file: str
    function: str
    form: str
    line: int


def _names(node: ast.AST) -> Iterator[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            yield sub.id
        elif isinstance(sub, ast.Attribute):
            yield sub.attr
        elif (
            isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) and isinstance(sub.slice.value, str)
        ):
            yield sub.slice.value


def _trayish(node: ast.AST) -> bool:
    return any(TRAYISH.search(name) for name in _names(node))


def _const(node: ast.AST, *values: int) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is int and node.value in values


def _forms(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.BinOp):
        op, left, right = node.op, node.left, node.right
        if (
            isinstance(op, ast.Add)
            and isinstance(left, ast.BinOp)
            and isinstance(left.op, ast.Mult)
            and (_const(left.right, 4) or _const(left.left, 4))
            and _trayish(node)
        ):
            yield "* 4 +"
        if (
            isinstance(op, ast.BitOr)
            and isinstance(left, ast.BinOp)
            and isinstance(left.op, ast.LShift)
            and _const(left.right, 8)
            and _trayish(node)
        ):
            yield "<< 8 |"
        if isinstance(op, ast.Add) and (_const(left, 254) or _const(right, 254)) and _trayish(node):
            yield "254 +"
        if isinstance(op, ast.FloorDiv) and _const(right, 4) and _trayish(left):
            yield "// 4"
        if isinstance(op, ast.Mod) and _const(right, 4) and _trayish(left):
            yield "% 4"
        if isinstance(op, ast.BitAnd) and _const(right, 3) and _trayish(left):
            yield "& 3"
        if isinstance(op, ast.RShift) and _const(right, 2) and _trayish(left):
            yield ">> 2"
        if isinstance(op, ast.RShift) and _const(right, 8) and _trayish(left):
            yield ">> 8"
        if isinstance(op, ast.BitAnd) and _const(right, 0xFF) and _trayish(left):
            yield "& 0xFF"
        if isinstance(op, ast.Sub) and _const(right, 254) and _trayish(left):
            yield "- 254"
        if isinstance(op, ast.Sub) and _const(right, 128) and _trayish(left):
            yield "- 128"
    elif isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        for op, a, b in zip(node.ops, operands, operands[1:], strict=False):
            if isinstance(op, ast.GtE) and _const(b, 254) and _trayish(a):
                yield ">= 254"
            if isinstance(op, ast.Lt) and _const(b, 254) and _trayish(a):
                yield "< 254"
            if isinstance(op, ast.GtE) and _const(b, 128) and _trayish(a):
                yield ">= 128"
        if (
            len(node.ops) == 2
            and _const(node.left, 128)
            and _const(node.comparators[1], 135)
            and _trayish(node.comparators[0])
        ):
            yield "128 <= x <= 135"
    elif isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "chr"
            and node.args
            and isinstance(node.args[0], ast.BinOp)
            and isinstance(node.args[0].op, ast.Add)
            and (_const(node.args[0].left, 65) or _const(node.args[0].right, 65))
        ):
            yield "chr(65 + …)"


def scan_file(path: Path, root: Path) -> list[Finding]:
    rel = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    findings: list[Finding] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, f"{scope}.{child.name}" if scope != "<module>" else child.name)
                continue
            for form in _forms(child):
                findings.append(Finding(rel, scope, form, child.lineno))
            visit(child, scope)

    visit(tree, "<module>")
    return findings


def scan(app_root: Path, repo_root: Path, *, skip: frozenset[str]) -> list[Finding]:
    found: list[Finding] = []
    for path in sorted(app_root.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel in skip:
            continue
        found.extend(scan_file(path, repo_root))
    return found
