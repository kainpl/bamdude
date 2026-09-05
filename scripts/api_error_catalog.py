"""Keep ``backend/app/data/api_errors_uk.json`` in step with the sentences the API refuses with.

The API answers a refusal with an English sentence in ``detail`` — ~700 raise sites,
written where the refusal is decided. Translating them happens once, at the
boundary (``backend/app/i18n/api_errors.py``): the exception handler looks the
English sentence up in the catalog and answers in the system language. The
English sentence *is* the key, so the raise sites never change and a missing
translation degrades to English, never to a bare key.

This script is the other half of that contract: it walks the backend's AST and
lists every sentence that can reach the wire, so the catalog can be checked
(``report``) and topped up (``sync``) mechanically, and a drift-guard test can
fail CI when a new refusal ships without its Ukrainian.

What counts as a sentence that can reach the wire:

* ``HTTPException(..., detail=<str | f-string | CONSTANT | a or b | {"message": ...}>)``
  and the positional form ``HTTPException(409, "...")``;
* ``JSONResponse(content={"detail": <...>})`` — a handful of routes build the
  refusal by hand;
* ``raise <DomainError>("...")`` for the exception classes routes forward as
  ``detail=str(exc)`` (``FORWARDED_EXCEPTIONS``);
* ``raise ValueError("...")`` anywhere under ``backend/app`` — many are forwarded
  the same way. These are collected as *optional*: the handler translates them
  when the catalog has them, but the guard does not demand them, because most
  ``ValueError`` messages never leave the process.

An f-string becomes a template with ``{}`` where a value is interpolated; the
handler matches the formatted sentence back against it with a regex.

Usage::

    python scripts/api_error_catalog.py report          # counts + what is missing
    python scripts/api_error_catalog.py sync [--prune]  # add missing keys as "", drop orphans
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "backend" / "app"
CATALOG_PATH = APP_DIR / "data" / "api_errors_uk.json"

sys.path.insert(0, str(ROOT))
from backend.app.i18n.api_errors import is_code  # noqa: E402

# Domain exceptions that routes forward verbatim (``detail=str(exc)``). Their
# messages are API-facing by construction, so the guard demands a translation.
FORWARDED_EXCEPTIONS = frozenset(
    {
        "BambuCloudError",
        "BambuCloudAuthError",
        "OrcaCloudError",
        "OrcaCloudAuthError",
        "PartStockError",
        "InvalidFilenameError",
        "DispatchEnqueueRejected",
        "SlicerTimeoutError",
        "SlicerInputError",
        "SlicerApiUnavailableError",
        "SlicerApiServerError",
        "OIDCIconError",
    }
)

# Kinds the guard requires a translation for. ``value`` is optional (see module doc).
REQUIRED_KINDS = frozenset({"http", "json", "forwarded"})

PLACEHOLDER = "{}"


@dataclass(frozen=True)
class Site:
    file: str
    line: int
    kind: str  # http | json | forwarded | value


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _template(node: ast.JoinedStr) -> str | None:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append(PLACEHOLDER)
    text = "".join(parts)
    # ``f"{e}"`` is a pure passthrough — nothing to translate.
    if text.replace(PLACEHOLDER, "").strip() == "":
        return None
    return text


def _sentences(expr: ast.expr | None, consts: dict[str, list[str]]) -> list[str]:
    """Every sentence an expression can evaluate to, or [] for opaque ones (``str(e)``)."""
    if expr is None:
        return []
    if isinstance(expr, ast.Constant):
        return [expr.value] if isinstance(expr.value, str) and expr.value.strip() else []
    if isinstance(expr, ast.JoinedStr):
        template = _template(expr)
        return [template] if template else []
    if isinstance(expr, ast.Name):
        return list(consts.get(expr.id, []))
    if isinstance(expr, ast.BoolOp):
        return [s for operand in expr.values for s in _sentences(operand, consts)]
    if isinstance(expr, ast.IfExp):
        return _sentences(expr.body, consts) + _sentences(expr.orelse, consts)
    if isinstance(expr, ast.Dict):
        for key, value in zip(expr.keys, expr.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "message":
                return _sentences(value, consts)
    return []


def _module_constants(tree: ast.Module) -> dict[str, list[str]]:
    consts: dict[str, list[str]] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Name):
                sentences = _sentences(value, {})
                if sentences:
                    consts[target.id] = sentences
    return consts


def _detail_expr(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "detail":
            return kw.value
    if len(call.args) >= 2:
        return call.args[1]
    return None


def _json_detail_expr(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "content" and isinstance(kw.value, ast.Dict):
            for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "detail":
                    return value
    return None


def scan(app_dir: Path = APP_DIR) -> dict[str, set[Site]]:
    """Map every sentence that can reach the wire to the sites that raise it."""
    # Constants are resolved per module first, then across modules for imported
    # names (``from .x import ERROR_TEXT``) — a name defined in two modules with
    # different texts is left unresolved rather than guessed.
    trees: dict[Path, ast.Module] = {}
    per_module: dict[Path, dict[str, list[str]]] = {}
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[path] = tree
        per_module[path] = _module_constants(tree)
    global_consts: dict[str, list[str]] = {}
    ambiguous: set[str] = set()
    for consts in per_module.values():
        for name, sentences in consts.items():
            if name in global_consts and global_consts[name] != sentences:
                ambiguous.add(name)
            global_consts.setdefault(name, sentences)
    for name in ambiguous:
        global_consts.pop(name, None)

    found: dict[str, set[Site]] = defaultdict(set)
    for path, tree in trees.items():
        consts = {**global_consts, **per_module[path]}
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name == "HTTPException":
                    kind, expr = "http", _detail_expr(node)
                elif name == "JSONResponse":
                    kind, expr = "json", _json_detail_expr(node)
                elif name == "json_error":
                    kind, expr = "json", (node.args[1] if len(node.args) >= 2 else None)
                else:
                    continue
            elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                name = _call_name(node.exc.func)
                if name in FORWARDED_EXCEPTIONS:
                    kind = "forwarded"
                elif name == "ValueError":
                    kind = "value"
                else:
                    continue
                expr = node.exc.args[0] if node.exc.args else None
            else:
                continue
            for sentence in _sentences(expr, consts):
                if is_code(sentence):
                    continue  # ``parts_not_editable`` — a code the frontend maps, not a sentence
                found[sentence].add(Site(rel, node.lineno, kind))
    return dict(found)


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_catalog(catalog: dict[str, str], path: Path = CATALOG_PATH) -> None:
    ordered = {k: catalog[k] for k in sorted(catalog, key=str.casefold)}
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def required_sentences(found: dict[str, set[Site]], machine_read: frozenset[str]) -> set[str]:
    return {s for s, sites in found.items() if any(x.kind in REQUIRED_KINDS for x in sites) and s not in machine_read}


def placeholders(text: str) -> int:
    return text.count(PLACEHOLDER)


def _machine_read() -> frozenset[str]:
    from backend.app.i18n.api_errors import MACHINE_READ  # noqa: PLC0415

    return MACHINE_READ


def _report(found: dict[str, set[Site]], catalog: dict[str, str], machine_read: frozenset[str]) -> int:
    by_kind: dict[str, set[str]] = defaultdict(set)
    for sentence, sites in found.items():
        for site in sites:
            by_kind[site.kind].add(sentence)
    print("sentences by kind:", {k: len(v) for k, v in sorted(by_kind.items())})
    required = required_sentences(found, machine_read)
    missing = sorted(s for s in required if not catalog.get(s))
    orphans = sorted(k for k in catalog if k not in found)
    untranslated_optional = sorted(
        s for s in found if s not in required and s not in machine_read and not catalog.get(s)
    )
    print(f"required: {len(required)}  translated: {len(required) - len(missing)}  missing: {len(missing)}")
    print(f"optional (ValueError) untranslated: {len(untranslated_optional)}  orphans in catalog: {len(orphans)}")
    for s in missing:
        print("  MISSING:", s)
    for s in orphans:
        print("  ORPHAN :", s)
    return 1 if missing or orphans else 0


def _sync(
    found: dict[str, set[Site]], catalog: dict[str, str], machine_read: frozenset[str], prune: bool, with_optional: bool
) -> int:
    wanted = set(found) if with_optional else required_sentences(found, machine_read)
    added = 0
    for sentence in wanted:
        if sentence in machine_read:
            continue
        if sentence not in catalog:
            catalog[sentence] = ""
            added += 1
    dropped = 0
    if prune:
        for key in [k for k in catalog if k not in found or k in machine_read]:
            del catalog[key]
            dropped += 1
    write_catalog(catalog)
    print(f"added {added} empty keys, dropped {dropped} orphans → {CATALOG_PATH.relative_to(ROOT).as_posix()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report")
    sync = sub.add_parser("sync")
    sync.add_argument("--prune", action="store_true", help="drop catalog keys no longer raised anywhere")
    sync.add_argument("--with-optional", action="store_true", help="also add the optional ValueError sentences")
    args = parser.parse_args(argv)

    found = scan()
    catalog = load_catalog()
    machine_read = _machine_read()
    if args.cmd == "report":
        return _report(found, catalog, machine_read)
    return _sync(found, catalog, machine_read, prune=args.prune, with_optional=args.with_optional)


if __name__ == "__main__":
    raise SystemExit(main())
