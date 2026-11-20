"""Static inventory of every writer of print_queue / printer_queues / auto_queue_items rows (G03 B, A13).

The scanner of the r5 evidence (``research-artifacts/g03-b-spec-r5-2026-11-19/writer_inventory.py``), kept here as
the A13 product guard's detection: ``backend/tests/unit/test_queue_writers_have_a_lock_class.py`` calls
``inventory()`` over ``backend/app`` and compares the functions it reports with ``classification.json`` beside this
file. Read-only. Parses the source with ``ast``; imports nothing from the application. Two changes against the
evidence copy: ``inventory()`` (the scan without the report; ``paths=False`` skips the entry-path search), and the
model names of an untyped site sorted — they followed the target dict's order, which the guard's determinism case
caught. It still runs on its own:

    python -m backend.tests.queue_writers.writer_inventory <backend/app> <out_dir> [--classify classification.json]
    python -m backend.tests.queue_writers.writer_inventory --selftest

r5 (review R21): a raw-SQL site's table is the table its DML verb names -- ``UPDATE <t>``, ``DELETE FROM <t>``,
``INSERT [OR x] INTO <t>``, ``REPLACE INTO <t>`` -- one site per (verb, table) of each string, never a table the
statement only reads (a subquery, ``UPDATE ... FROM <t>``, ``DELETE ... USING <t>``); comments and string literals
are removed first, and ``FOR [NO KEY] UPDATE`` / ``DO UPDATE`` / ``ON UPDATE`` / ``ON DELETE`` are not verbs. A
``SELECT ... FROM <target> ... FOR [NO KEY] UPDATE|SHARE`` string is a ``lock-raw`` site. ``--selftest`` runs the
pinned statement cases (UPDATE with a subquery, multi-table DELETE, ...). Every ordering that reaches the output is
sorted, so two runs under different hash seeds give byte-identical JSON. With ``--classify`` the run exits 1 when a
function with a site is unclassified or an entry names no function (stale).

What counts as a site (one AST node):
  * an attribute assignment / aug-assignment on an expression whose type is inferred as an instance of a target
    model (``item.status = ...``), or -- with ``confidence=candidate`` -- on an expression of unknown type whose
    attribute is a column / relationship of a target model and that is not inferred as another model;
  * ``setattr(obj, ...)`` on a target instance (certain) or on an unknown object in a module that names a target
    model (candidate);
  * ``update(M)`` / ``delete(M)`` / ``insert(M)`` (any sqlalchemy alias, ``M.__table__.update()`` too); an UPDATE
    whose ``.values(...)`` only re-assigns columns to themselves is reported as kind ``lock-noop-update``;
  * ``take_write_lock(db, M.__table__, ...)`` (a lock, reported as ``lock-take-write``);
  * a constructor ``M(...)``;
  * ``db.delete(x)`` / ``db.add(x)`` / ``db.merge(x)`` with ``x`` a target instance;
  * a ``text(...)`` / ``exec_driver_sql`` string whose UPDATE / DELETE / INSERT verb names a target table (r5),
    and a migration ``recreate_table(conn, "<target>", ...)`` call;
  * a call of a *writer helper*: any function of the code base that itself contains a direct site above, plus the
    seed list ``SEED_HELPERS`` -- found transitively (a helper of a helper is a helper), reported as kind
    ``helper-call`` with the helper it reaches.
For every site: file, line, enclosing function, kind, model, columns, confidence, the lock scopes it lies in
lexically, the scopes / sessions / commits of its function, its immediate resolved callers and paths up to entry
points (route decorators, Telegram handlers, functions with no static caller).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

TARGETS = {
    "PrintQueueItem": "print_queue",
    "PrinterQueue": "printer_queues",
    "AutoQueueItem": "auto_queue_items",
}
TARGET_TABLES = set(TARGETS.values())
SEED_HELPERS = {
    "set_queue_idle",
    "set_queue_printing",
    "halt_lane",
    "update_queue_counters",
    "set_status",
    "set_status_for_batch",
    "defer_claim",
    "detach_print_queue_refs",
    "detach_auto_queue_refs",
}
LOCK_SCOPES = {
    "queue_claim_scope",
    "claim_lanes",
    "cancellation_scope",
    "queue_scope_lock",
    "reusing_sources",
    "archive_write_scope",
    "storage_mutation",
    "queue_process_lanes",
    "archive_process_lock",
    "queue_claim_locks",
    "begin_nested",
    "product_gate",
    "lock_lanes",
    "library_delete_scope",
    "archive_file_reference_scope",
    # G03 B1: the light writers' fresh session under claim_lanes (queue_ops.lane_session).
    "lane_session",
}
SESSION_OPENERS = {"async_session", "async_session_maker", "AsyncSessionLocal", "session_factory", "get_session"}
STMT_METHODS = {
    "where",
    "order_by",
    "options",
    "execution_options",
    "with_for_update",
    "limit",
    "join",
    "outerjoin",
    "filter",
    "filter_by",
    "distinct",
    "offset",
    "group_by",
    "having",
    "select_from",
    "returning",
    "correlate",
}
INST_METHODS = {"scalar_one_or_none", "scalar_one", "scalar", "first", "one", "one_or_none"}
COLL_METHODS = {"all", "unique", "fetchall", "scalars"}


@dataclass
class Site:
    id: str
    file: str
    line: int
    func: str
    kind: str
    model: str
    columns: list[str]
    confidence: str
    receiver: str = ""
    helper: str = ""
    lexical_scopes: list[str] = field(default_factory=list)
    after_commit_in_scope: bool = False
    func_scopes: list[str] = field(default_factory=list)
    func_opens_session: bool = False
    func_commits: bool = False
    callers: list[str] = field(default_factory=list)
    entry_paths: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- models


def parse_models(app: Path):
    """Class name -> {"table", "columns", "relationships": {attr: target_class}} for every ORM class."""
    models = {}
    for path in sorted((app / "models").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(st, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in st.targets)
                for st in node.body
            ):
                _MODEL_NAMES.add(node.name)
    for path in sorted((app / "models").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            table, cols, rels = None, set(), {}
            for stmt in node.body:
                target = None
                value = None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target, value = stmt.targets[0].id, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target, value = stmt.target.id, stmt.value
                if target is None:
                    continue
                if target == "__tablename__" and isinstance(value, ast.Constant):
                    table = value.value
                    continue
                if isinstance(value, ast.Call):
                    fname = _call_name(value)
                    if fname in {"mapped_column", "Column"}:
                        cols.add(target)
                    elif fname == "relationship":
                        tgt = None
                        if value.args and isinstance(value.args[0], ast.Constant):
                            tgt = value.args[0].value
                        if tgt is None and isinstance(stmt, ast.AnnAssign):
                            tgt = _annotation_model(stmt.annotation)
                        rels[target] = (tgt, _rel_is_collection(stmt, value))
            if table:
                models[node.name] = {"table": table, "columns": cols, "relationships": rels}
    return models


def _rel_is_collection(stmt, value) -> bool:
    for kw in value.keywords:
        if kw.arg == "lazy" and isinstance(kw.value, ast.Constant) and kw.value.value == "dynamic":
            return True
        if kw.arg == "uselist" and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    if isinstance(stmt, ast.AnnAssign):
        text = ast.unparse(stmt.annotation)
        return "list[" in text or "List[" in text
    return False


_MODEL_NAMES: set[str] = set()


def model_in_text(text: str) -> str | None:
    """The ORM class a type annotation names, by whole word (``PrintQueueItemResponse`` is not ``PrintQueueItem``)."""
    text = text.replace('"', "").replace("'", "")
    for name in sorted(_MODEL_NAMES, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])", text):
            return name
    return None


def models_in_text(text: str) -> str | None:
    """Every ORM class an annotation names, joined ``A|B`` (``list[tuple[PrintQueueItem | AutoQueueItem, bool]]``)."""
    text = text.replace('"', "").replace("'", "")
    found = [n for n in sorted(_MODEL_NAMES) if re.search(rf"(?<![A-Za-z0-9_]){n}(?![A-Za-z0-9_])", text)]
    return "|".join(found) or None


def _annotation_model(node) -> str | None:
    return model_in_text(ast.unparse(node)) if node is not None else None


def _call_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


MODELS_ALL: dict = {}

# ---------------------------------------------------------------- raw SQL (r5, R21)

_SQL_TOKEN = re.compile(r'"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w.]*|\S')
_NOT_A_VERB_AFTER = {"for", "do", "key", "on"}  # FOR [NO KEY] UPDATE, DO UPDATE SET, ON UPDATE / ON DELETE


def _sql_tokens(sql: str) -> list[str]:
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"'(?:[^']|'')*'", " '' ", s)
    return _SQL_TOKEN.findall(s)


def _table_name(token: str) -> str:
    name = token.strip('"`[]').lower()
    return name.rsplit(".", 1)[-1]  # schema-qualified "public.print_queue"


def dml_targets(sql: str) -> list[tuple[str, str]]:
    """``(verb, table)`` for every DML statement in ``sql``: the table its verb names, never one it only reads."""
    toks = _sql_tokens(sql)
    low = [t.lower() for t in toks]
    out = []
    for i, t in enumerate(low):
        prev = low[i - 1] if i else ""
        j = None
        verb = None
        if t == "update" and prev not in _NOT_A_VERB_AFTER:
            verb, j = "update", i + 1
            if j < len(low) and low[j] == "or":  # SQLite UPDATE OR IGNORE
                j += 2
            if j < len(low) and low[j] == "only":
                j += 1
        elif t == "delete" and i + 1 < len(low) and low[i + 1] == "from" and prev not in _NOT_A_VERB_AFTER:
            verb, j = "delete", i + 2
            if j < len(low) and low[j] == "only":
                j += 1
        elif t in {"insert", "replace"}:
            k = i + 1
            if t == "insert" and k < len(low) and low[k] == "or":  # INSERT OR REPLACE / IGNORE
                k += 2
            if k < len(low) and low[k] == "into":
                verb, j = "insert", k + 1
        if verb and j is not None and j < len(toks) and re.match(r'["`\[A-Za-z_]', toks[j]):
            out.append((verb, _table_name(toks[j])))
    return sorted(set(out))


def raw_lock_targets(sql: str) -> list[tuple[str, str]]:
    """``(mode, table)`` for a ``SELECT ... FROM <t> ... FOR [NO KEY] UPDATE|[KEY] SHARE`` string (first FROM)."""
    toks = _sql_tokens(sql)
    low = [t.lower() for t in toks]
    out = []
    for i, t in enumerate(low):
        if t != "for" or i + 1 >= len(low):
            continue
        rest = low[i + 1 : i + 4]
        if rest[:1] == ["update"]:
            mode = "for-update"
        elif rest[:3] == ["no", "key", "update"]:
            mode = "for-no-key-update"
        elif rest[:1] == ["share"]:
            mode = "for-share"
        elif rest[:2] == ["key", "share"]:
            mode = "for-key-share"
        else:
            continue
        froms = [k for k in range(i) if low[k] == "from" and k + 1 < len(toks)]
        if froms:
            out.append((mode, _table_name(toks[froms[0] + 1])))
    return sorted(set(out))


SELFTEST_CASES = [
    (
        "UPDATE printer_queues SET pending_count = (SELECT COUNT(*) FROM print_queue WHERE queue_id = 1)",
        [("update", "printer_queues")],
        [],
    ),
    (
        "UPDATE printer_queues SET status = 'idle' FROM print_queue WHERE print_queue.queue_id = printer_queues.id",
        [("update", "printer_queues")],
        [],
    ),
    (
        "DELETE FROM auto_queue_items WHERE id IN (SELECT source_auto_item_id FROM print_queue)",
        [("delete", "auto_queue_items")],
        [],
    ),
    (
        "DELETE FROM auto_queue_items USING print_queue WHERE print_queue.source_auto_item_id = auto_queue_items.id",
        [("delete", "auto_queue_items")],
        [],
    ),
    ("INSERT INTO print_queue (queue_id) SELECT 1 FROM auto_queue_items", [("insert", "print_queue")], []),
    ("INSERT INTO settings (k, v) VALUES ('a', 1) ON CONFLICT (k) DO UPDATE SET v = 2", [("insert", "settings")], []),
    ("SELECT id FROM print_queue WHERE id = :id FOR UPDATE", [], [("for-update", "print_queue")]),
    ("SELECT id FROM printer_queues WHERE id = :id FOR NO KEY UPDATE", [], [("for-no-key-update", "printer_queues")]),
    ("CREATE TABLE t (q INTEGER REFERENCES printer_queues(id) ON DELETE SET NULL ON UPDATE CASCADE)", [], []),
    (
        "UPDATE print_archives SET queue_id = NULL WHERE queue_id IN (SELECT id FROM printer_queues)",
        [("update", "print_archives")],
        [],
    ),
    ("-- update print_queue set x = 1\nSELECT 1", [], []),
    ("SELECT 'update print_queue set x = 1'", [], []),
    (
        'UPDATE "print_queue" SET x = 1; DELETE FROM printer_queues WHERE id = 1',
        [("delete", "printer_queues"), ("update", "print_queue")],
        [],
    ),
    ("INSERT OR REPLACE INTO auto_queue_items (id) VALUES (1)", [("insert", "auto_queue_items")], []),
    ("UPDATE OR IGNORE print_queue SET x = 1", [("update", "print_queue")], []),
    (
        "WITH moved AS (UPDATE print_queue SET x = 1 RETURNING id) DELETE FROM auto_queue_items WHERE id IN (SELECT id FROM moved)",
        [("delete", "auto_queue_items"), ("update", "print_queue")],
        [],
    ),
    ("UPDATE public.print_queue SET x = 1", [("update", "print_queue")], []),
]


def selftest() -> int:
    bad = 0
    for sql, want_dml, want_lock in SELFTEST_CASES:
        got_dml, got_lock = dml_targets(sql), raw_lock_targets(sql)
        ok = got_dml == sorted(want_dml) and got_lock == sorted(want_lock)
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} dml={got_dml} lock={got_lock} :: {sql[:90]}")
    print(f"selftest cases={len(SELFTEST_CASES)} failed={bad}")
    return 1 if bad else 0


# ---------------------------------------------------------------- module index


class ModuleInfo:
    def __init__(self, name: str, path: Path, rel: str, tree: ast.Module):
        self.name = name
        self.path = path
        self.rel = rel
        self.tree = tree
        self.symbols: dict[str, tuple[str, str]] = {}  # local alias -> (module, name)
        self.module_aliases: dict[str, str] = {}  # local alias -> module
        self.sqla: dict[str, str] = {}  # local alias -> update/delete/insert/select/text
        self.model_aliases: dict[str, str] = {}  # local alias -> model class
        self.functions: dict[str, ast.AST] = {}  # qualname -> node
        self.classes: dict[str, ast.ClassDef] = {}
        self.router_prefix: dict[str, str] = {}
        self.mentions_target = False


def module_name(app: Path, path: Path) -> str:
    rel = path.relative_to(app.parent.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def index_modules(app: Path) -> dict[str, ModuleInfo]:
    mods = {}
    for path in sorted(app.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        name = module_name(app, path)
        info = ModuleInfo(name, path, str(path.relative_to(app.parent.parent)).replace("\\", "/"), tree)
        mods[name] = info
    for info in mods.values():
        _collect_imports(info)
        _collect_defs(info)
    return mods


def _resolve_relative(info: ModuleInfo, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    base = info.name.split(".")
    if info.path.name != "__init__.py":
        base = base[:-1]
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join(base + ([node.module] if node.module else []))


def _collect_imports(info: ModuleInfo):
    for node in ast.walk(info.tree):
        if isinstance(node, ast.ImportFrom):
            mod = _resolve_relative(info, node)
            for alias in node.names:
                local = alias.asname or alias.name
                if mod.startswith("sqlalchemy") and alias.name in {"update", "delete", "insert", "select", "text"}:
                    info.sqla[local] = alias.name
                    continue
                if alias.name in MODELS_ALL and mod.startswith("backend.app.models"):
                    info.model_aliases[local] = alias.name
                info.symbols[local] = (mod, alias.name)
                info.module_aliases.setdefault(local, f"{mod}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    info.module_aliases[alias.asname] = alias.name
                else:
                    info.module_aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]
    for name in MODELS_ALL:
        info.model_aliases.setdefault(name, name) if _defines_class(info, name) else None
    src = info.path.read_text(encoding="utf-8")
    info.mentions_target = any(t in src for t in TARGETS) or any(t in src for t in TARGET_TABLES)


def _defines_class(info: ModuleInfo, name: str) -> bool:
    return any(isinstance(n, ast.ClassDef) and n.name == name for n in info.tree.body)


def _collect_defs(info: ModuleInfo):
    def visit(body, prefix, cls=None):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}{node.name}"
                info.functions[q] = node
                node._qual = q  # type: ignore[attr-defined]
                node._cls = cls  # type: ignore[attr-defined]
                visit(node.body, q + ".", cls)
            elif isinstance(node, ast.ClassDef):
                info.classes[node.name] = node
                visit(node.body, f"{prefix}{node.name}.", node.name)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
                for sub in ("body", "orelse", "finalbody"):
                    visit(getattr(node, sub, []) or [], prefix, cls)
                for h in getattr(node, "handlers", []) or []:
                    visit(h.body, prefix, cls)

    visit(info.tree.body, "")
    for node in info.tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _call_name(node.value) == "APIRouter":
            prefix = ""
            for kw in node.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = kw.value.value
            for t in node.targets:
                if isinstance(t, ast.Name):
                    info.router_prefix[t.id] = prefix
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _call_name(node.value) == "Router":
            for t in node.targets:
                if isinstance(t, ast.Name):
                    info.router_prefix[t.id] = "<telegram>"


# ---------------------------------------------------------------- typing inside a function


def _is_model_name(info: ModuleInfo, node) -> str | None:
    if isinstance(node, ast.Name) and node.id in info.model_aliases:
        return info.model_aliases[node.id]
    if isinstance(node, ast.Attribute) and node.attr in MODELS_ALL and isinstance(node.value, ast.Name):
        return node.attr
    return None


class Typer:
    """Flow-insensitive local type inference: name -> (kind, model) with kind in inst/coll/stmt/result/map."""

    def __init__(self, info: ModuleInfo, func, returns: dict):
        self.info = info
        self.env: dict[str, tuple[str, str]] = {}
        self.returns = returns
        self.func = func
        self._params()
        for _ in range(4):  # a few passes reach a fixpoint for chains of assignments
            self._pass()

    def _params(self):
        args = self.func.args
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if a.annotation is None:
                continue
            text = ast.unparse(a.annotation)
            model = model_in_text(text)
            if model:
                coll = any(k in text for k in ("list[", "List[", "Sequence[", "Iterable[", "set[", "tuple["))
                self.env[a.arg] = ("coll" if coll else "inst", model)

    def _bind(self, target, typ):
        if typ is None:
            return
        if isinstance(target, ast.Name):
            self.env[target.id] = typ

    def _pass(self):
        for node in ast.walk(self.func):
            if isinstance(node, ast.Assign):
                typ = self.type_of(node.value)
                for t in node.targets:
                    self._bind(t, typ)
                    if isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple):
                        for et, ev in zip(t.elts, node.value.elts, strict=False):
                            self._bind(et, self.type_of(ev))
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                typ = self.type_of(node.value)
                if typ is None:
                    text = ast.unparse(node.annotation)
                    name = model_in_text(text)
                    if name:
                        coll = "list[" in text or "set[" in text
                        typ = ("coll" if coll else "inst", name)
                self._bind(node.target, typ)
            elif isinstance(node, ast.NamedExpr):
                self._bind(node.target, self.type_of(node.value))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and self._bind_model_loop(node):
                pass
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                it = node.iter
                target = node.target
                if isinstance(it, ast.Call) and _call_name(it) == "enumerate" and it.args:
                    it = it.args[0]
                    if isinstance(target, ast.Tuple) and len(target.elts) == 2:
                        target = target.elts[1]
                typ = self.type_of(it)
                if typ and typ[0] == "colltuple" and isinstance(target, ast.Tuple) and target.elts:
                    self._bind(target.elts[0], ("inst", typ[1]))
                elif typ and typ[0] in {"coll", "result"}:
                    self._bind(target, ("inst", typ[1]))
                elif typ and typ[0] == "map":
                    pass
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                pass

    def _bind_model_loop(self, node) -> bool:
        """``for model in (A, B)`` / ``for kind, model in (("a", A), ("b", B))``: a model variable."""
        it, target = node.iter, node.target
        if not isinstance(it, (ast.Tuple, ast.List)) or not it.elts:
            return False
        bound = False
        if isinstance(target, ast.Name):
            names = [_is_model_name(self.info, e) for e in it.elts]
            if all(names):
                self.env[target.id] = ("models", "|".join(names))
                return True
            return False
        if isinstance(target, ast.Tuple) and all(isinstance(e, ast.Tuple) for e in it.elts):
            for pos, t in enumerate(target.elts):
                if not isinstance(t, ast.Name):
                    continue
                names = [_is_model_name(self.info, e.elts[pos]) if pos < len(e.elts) else None for e in it.elts]
                if all(names):
                    self.env[t.id] = ("models", "|".join(names))
                    bound = True
        return bound

    def model_of(self, node) -> str | None:
        """The model (or ``A|B`` union) an expression names as a class."""
        m = _is_model_name(self.info, node)
        if m:
            return m
        if isinstance(node, ast.Name) and self.env.get(node.id, ("",))[0] == "models":
            return self.env[node.id][1]
        if isinstance(node, ast.IfExp):
            a, b = self.model_of(node.body), self.model_of(node.orelse)
            if a and b:
                return f"{a}|{b}"
        if isinstance(node, ast.Attribute) and node.attr == "__table__":
            return self.model_of(node.value)
        return None

    def type_of(self, node):  # noqa: C901
        info = self.info
        if node is None:
            return None
        if isinstance(node, ast.Await):
            return self.type_of(node.value)
        if isinstance(node, ast.Name):
            return self.env.get(node.id)
        if isinstance(node, ast.IfExp):
            m = self.model_of(node)
            if m:
                return ("models", m)
            return self.type_of(node.body) or self.type_of(node.orelse)
        if isinstance(node, ast.BoolOp):
            for v in node.values:
                t = self.type_of(v)
                if t:
                    return t
            return None
        if isinstance(node, ast.Subscript):
            t = self.type_of(node.value)
            if t and t[0] in {"coll", "map"}:
                if isinstance(node.slice, ast.Slice):
                    return t
                return ("inst", t[1])
            return None
        if isinstance(node, ast.Attribute):
            t = self.type_of(node.value)
            if t and t[0] == "inst" and "|" not in t[1]:
                rel = MODELS_ALL.get(t[1], {}).get("relationships", {}).get(node.attr)
                if rel and rel[0] in MODELS_ALL:
                    return ("coll" if rel[1] else "inst", rel[0])
            return None
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            sub = Typer.__new__(Typer)
            sub.info, sub.env, sub.returns, sub.func = self.info, dict(self.env), self.returns, self.func
            for gen in node.generators:
                it = sub.type_of(gen.iter)
                if it and it[0] in {"coll", "result"}:
                    sub._bind(gen.target, ("inst", it[1]))
            et = sub.type_of(node.elt)
            if et and et[0] == "inst":
                return ("coll", et[1])
            return None
        if isinstance(node, ast.DictComp):
            sub = Typer.__new__(Typer)
            sub.info, sub.env, sub.returns, sub.func = self.info, dict(self.env), self.returns, self.func
            for gen in node.generators:
                it = sub.type_of(gen.iter)
                if it and it[0] in {"coll", "result"}:
                    sub._bind(gen.target, ("inst", it[1]))
            vt = sub.type_of(node.value)
            if vt and vt[0] == "inst":
                return ("map", vt[1])
            return None
        if isinstance(node, ast.Call):
            f = node.func
            model = _is_model_name(info, f)
            if model:
                return ("inst", model)
            if isinstance(f, ast.Name):
                if info.sqla.get(f.id) == "select" or f.id == "select":
                    if node.args:
                        m = self.model_of(node.args[0])
                        if m:
                            return ("stmt", m)
                    return None
                if f.id in {"list", "sorted", "tuple", "set", "reversed"} and node.args:
                    t = self.type_of(node.args[0])
                    if t and t[0] in {"coll", "result"}:
                        return ("coll", t[1])
                    return None
                rt = self.returns.get((info.name, f.id)) or self.returns.get(info.symbols.get(f.id, ("", ""))[0:2])
                if rt:
                    return rt
                return None
            if isinstance(f, ast.Attribute):
                base_t = self.type_of(f.value)
                if f.attr in STMT_METHODS and base_t and base_t[0] == "stmt":
                    return base_t
                if f.attr == "execute" and node.args:
                    t = self.type_of(node.args[0])
                    if t and t[0] == "stmt":
                        return ("result", t[1])
                    return None
                if f.attr == "scalars" and node.args:
                    t = self.type_of(node.args[0])
                    if t and t[0] == "stmt":
                        return ("coll", t[1])
                if f.attr == "scalar" and node.args:
                    t = self.type_of(node.args[0])
                    if t and t[0] == "stmt":
                        return ("inst", t[1])
                if f.attr == "get" and node.args:
                    m = self.model_of(node.args[0])
                    if m:
                        return ("inst", m)
                    if base_t and base_t[0] == "map":
                        return ("inst", base_t[1])
                if f.attr == "values" and base_t and base_t[0] == "map":
                    return ("coll", base_t[1])
                if base_t and base_t[0] == "result":
                    if f.attr in INST_METHODS:
                        return ("inst", base_t[1])
                    if f.attr in COLL_METHODS:
                        return ("coll", base_t[1])
                if base_t and base_t[0] == "coll":
                    if f.attr in {"all", "unique", "fetchall", "scalars"}:
                        return base_t
                    if f.attr in {"first", "one", "one_or_none", "pop"}:
                        return ("inst", base_t[1])
                # module.function(...) with a known return annotation
                if isinstance(f.value, ast.Name) and f.value.id in info.module_aliases:
                    rt = self.returns.get((info.module_aliases[f.value.id], f.attr))
                    if rt:
                        return rt
        return None


# ---------------------------------------------------------------- scanning


def return_types(mods):
    out = {}
    for info in mods.values():
        for q, fn in info.functions.items():
            if fn.returns is None or "." in q:
                continue
            text = ast.unparse(fn.returns).replace('"', "")
            name = models_in_text(text)
            if name and not text.startswith(("tuple", "dict", "Tuple", "Dict")):
                coll = any(k in text for k in ("list[", "List[", "Sequence[", "set["))
                kind = "coll" if coll else "inst"
                if coll and "tuple[" in text:
                    kind = "colltuple"  # rows of (model, ...): a for-loop's first target is the row
                out[(info.name, fn.name)] = (kind, name)
    return out


def parents_of(tree):
    par = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            par[child] = node
    return par


def own_nodes(fn):
    """Nodes of fn's body that do not belong to a nested function or class."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.append(child)


def _values_keys(node, par):
    """The keyword keys of the .values(...) in the method chain around a Core statement node."""
    keys, cur = [], node
    while cur in par:
        p = par[cur]
        if isinstance(p, ast.Attribute) and p.value is cur:
            cur = p
            continue
        if isinstance(p, ast.Call) and p.func is cur:
            if isinstance(cur, ast.Attribute) and cur.attr == "values":
                for kw in p.keywords:
                    keys.append(kw.arg or "**")
                for a in p.args:
                    if isinstance(a, ast.Dict):
                        keys += [k.value if isinstance(k, ast.Constant) else "<dyn>" for k in a.keys if k is not None]
                    else:
                        keys.append("<dyn>")
                noop = (
                    all(isinstance(kw.value, ast.Attribute) and kw.value.attr == kw.arg for kw in p.keywords)
                    and p.keywords
                    and not p.args
                )
                if noop:
                    keys = ["<noop>"] + keys
            cur = p
            continue
        break
    return keys


def _stack_scopes(stack_node, before_line):
    """Lock scopes entered into an ``AsyncExitStack`` block (``enter_async_context(claim_lanes(...))``) before a line."""
    found = []
    for sub in ast.walk(stack_node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "enter_async_context"
            and sub.args
            and isinstance(sub.args[0], ast.Call)
            and _call_name(sub.args[0]) in LOCK_SCOPES
            and sub.lineno <= before_line
        ):
            found.append("stack:" + ast.unparse(sub.args[0])[:74])
    return found


def lexical_scopes(node, par, fn):
    out = []
    cur = node
    while cur in par and cur is not fn:
        cur = par[cur]
        if isinstance(cur, (ast.AsyncWith, ast.With)) and any(
            isinstance(i.context_expr, ast.Call) and _call_name(i.context_expr) == "AsyncExitStack" for i in cur.items
        ):
            out.extend(reversed(_stack_scopes(cur, node.lineno)))
        if isinstance(cur, (ast.AsyncWith, ast.With)):
            for item in cur.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call):
                    name = _call_name(expr)
                    if name in LOCK_SCOPES:
                        out.append(ast.unparse(expr)[:80])
                    elif name in SESSION_OPENERS:
                        out.append(f"session:{name}")
                elif isinstance(expr, ast.Subscript) and "_queue_claim_locks" in ast.unparse(expr):
                    out.append(ast.unparse(expr)[:80])
    return list(reversed(out))


LANE_SCOPES = {
    "queue_claim_scope",
    "claim_lanes",
    "cancellation_scope",
    "queue_scope_lock",
    "reusing_sources",
    "library_delete_scope",
    "lane_session",
}


def after_commit_in_lane_scope(node, par, fn) -> bool:
    """A site inside a lane scope's block that follows a ``commit()`` in the same block: the scope's database guard
    (transaction-scoped on PostgreSQL) ended at that commit, only the in-process lock may remain."""
    cur = node
    while cur in par and cur is not fn:
        cur = par[cur]
        if isinstance(cur, (ast.AsyncWith, ast.With)) and any(
            isinstance(i.context_expr, ast.Call) and _call_name(i.context_expr) in LANE_SCOPES for i in cur.items
        ):
            for sub in ast.walk(cur):
                if isinstance(sub, ast.Call) and _call_name(sub) == "commit" and sub.lineno < node.lineno:
                    return True
            return False
    return False


def func_facts(fn):
    scopes, opens, commits = set(), False, False
    for node in own_nodes(fn):
        if isinstance(node, (ast.AsyncWith, ast.With)):
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call):
                    name = _call_name(expr)
                    if name in LOCK_SCOPES:
                        scopes.add(name)
                    if name in SESSION_OPENERS:
                        opens = True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if (
                name == "enter_async_context"
                and node.args
                and isinstance(node.args[0], ast.Call)
                and _call_name(node.args[0]) in LOCK_SCOPES
            ):
                scopes.add(_call_name(node.args[0]))
            if name == "commit":
                commits = True
            if name in SESSION_OPENERS:
                opens = True
    return sorted(scopes), opens, commits


def scan(mods):
    returns = return_types(mods)
    target_cols = {m: MODELS_ALL[m]["columns"] | set(MODELS_ALL[m]["relationships"]) for m in TARGETS}
    all_target_cols = set().union(*target_cols.values())
    direct = []  # (info, qual, node, kind, model, cols, confidence, receiver)
    for info in mods.values():
        for q, fn in info.functions.items():
            typer = Typer(info, fn, returns)
            par = parents_of(fn)
            for node in own_nodes(fn):
                # attribute writes
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                flat = []
                for t in targets:
                    flat += t.elts if isinstance(t, ast.Tuple) else [t]
                for t in flat:
                    if not isinstance(t, ast.Attribute):
                        continue
                    rt = typer.type_of(t.value)
                    recv = ast.unparse(t.value)
                    if rt and rt[0] == "inst" and any(x in TARGETS for x in rt[1].split("|")):
                        direct.append((info, q, t, "orm-attr", rt[1], [t.attr], "certain", recv))
                    elif rt is None and t.attr in all_target_cols and not recv.startswith(("self", "cls")):
                        models = sorted(m for m in TARGETS if t.attr in target_cols[m])  # never the dict order
                        if info.mentions_target or recv in {"item", "row", "queue", "job", "auto", "queue_item"}:
                            direct.append((info, q, t, "orm-attr", "|".join(models), [t.attr], "candidate", recv))
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                f = node.func
                # setattr
                if isinstance(f, ast.Name) and f.id == "setattr" and node.args:
                    rt = typer.type_of(node.args[0])
                    col = ast.unparse(node.args[1]) if len(node.args) > 1 else "?"
                    recv = ast.unparse(node.args[0])
                    if rt and rt[0] == "inst" and any(x in TARGETS for x in rt[1].split("|")):
                        direct.append((info, q, node, "setattr", rt[1], [col], "certain", recv))
                    elif rt is None and info.mentions_target:
                        direct.append((info, q, node, "setattr", "?", [col], "candidate", recv))
                    continue
                # Core statements
                core = None
                if isinstance(f, ast.Name) and info.sqla.get(f.id) in {"update", "delete", "insert"} and node.args:
                    core = info.sqla[f.id]
                    m = typer.model_of(node.args[0])
                elif (
                    isinstance(f, ast.Attribute)
                    and f.attr in {"update", "delete", "insert"}
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "__table__"
                ):
                    core = f.attr
                    m = typer.model_of(f.value.value)
                if core:
                    if m and any(x in TARGETS for x in m.split("|")):
                        keys = _values_keys(node, par)
                        kind = f"core-{core}"
                        if keys and keys[0] == "<noop>":
                            kind, keys = "lock-noop-update", keys[1:]
                        direct.append((info, q, node, kind, m, keys, "certain", ""))
                    continue
                if name in {"take_write_lock", "lock_nowait", "lock_nowait_where"} and len(node.args) >= 2:
                    m = typer.model_of(node.args[1])
                    if m and any(x in TARGETS for x in m.split("|")):
                        direct.append((info, q, node, f"lock-{name}", m, [], "certain", ""))
                    continue
                if isinstance(f, ast.Attribute) and f.attr == "with_for_update":
                    m = _select_model(typer, f.value)
                    if m and any(x in TARGETS for x in m.split("|")):
                        opts = ",".join(f"{kw.arg}" for kw in node.keywords if kw.arg)
                        direct.append((info, q, node, "lock-for-update", m, [opts] if opts else [], "certain", ""))
                    continue
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr == "get"
                    and node.args
                    and any(kw.arg == "with_for_update" for kw in node.keywords)
                ):
                    m = typer.model_of(node.args[0])
                    if m and any(x in TARGETS for x in m.split("|")):
                        direct.append((info, q, node, "lock-for-update", m, ["db.get"], "certain", ""))
                        continue
                # constructor
                m = _is_model_name(info, f)
                if m in TARGETS:
                    cols = [kw.arg or "**" for kw in node.keywords]
                    direct.append((info, q, node, "construct", m, cols, "certain", ""))
                    continue
                # session delete / add / merge
                if isinstance(f, ast.Attribute) and f.attr in {"delete", "add", "merge", "add_all"} and node.args:
                    rt = typer.type_of(node.args[0])
                    if rt and any(x in TARGETS for x in rt[1].split("|")) and (rt[0] == "inst" or f.attr == "add_all"):
                        direct.append(
                            (info, q, node, f"session-{f.attr}", rt[1], [], "certain", ast.unparse(node.args[0]))
                        )
                    elif (
                        rt is None
                        and f.attr == "delete"
                        and info.mentions_target
                        and not isinstance(node.args[0], ast.Call)
                    ):
                        recv = ast.unparse(node.args[0])
                        if recv in {"item", "row", "job", "auto", "queue", "queue_item", "auto_item", "q"}:
                            direct.append((info, q, node, "session-delete", "?", [], "candidate", recv))
                # r5: a migration helper that copies a table named by a string (``recreate_table(conn, "t", ...)``)
                if (
                    name == "recreate_table"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and str(node.args[1].value) in TARGET_TABLES
                ):
                    direct.append(
                        (info, q, node, "raw-sql", str(node.args[1].value), ["recreate"], "certain", "recreate_table")
                    )
                    continue
                # raw SQL (r5, R21): the table each DML verb names; a FOR UPDATE / SHARE read is a lock site
                if name in {"text", "exec_driver_sql"} and node.args and isinstance(node.args[0], ast.Constant):
                    s = str(node.args[0].value)
                    for verb, tbl in dml_targets(s):
                        if tbl in TARGET_TABLES:
                            direct.append((info, q, node, "raw-sql", tbl, [verb], "certain", " ".join(s.split())[:60]))
                    for mode, tbl in raw_lock_targets(s):
                        if tbl in TARGET_TABLES:
                            direct.append((info, q, node, "lock-raw", tbl, [mode], "certain", " ".join(s.split())[:60]))
    return direct


def _select_model(typer, node):
    """The model of the innermost ``select(...)`` of a statement chain (``select(M)`` or ``select(M.col)``)."""
    cur = node
    while True:
        if isinstance(cur, ast.Call):
            fname = _call_name(cur)
            if fname == "select" and cur.args:
                a = cur.args[0]
                m = typer.model_of(a)
                if m is None and isinstance(a, ast.Attribute):
                    m = typer.model_of(a.value)
                return m
            cur = cur.func
            continue
        if isinstance(cur, ast.Attribute):
            cur = cur.value
            continue
        if isinstance(cur, ast.Name):
            t = typer.env.get(cur.id)
            if t and t[0] == "stmt":
                return t[1]
        if isinstance(cur, ast.IfExp):
            return _select_model(typer, cur.body)
        return None


# ---------------------------------------------------------------- call graph


def build_graph(mods):
    by_name = defaultdict(list)  # bare name -> [(module, qual)]
    methods = defaultdict(list)
    for info in mods.values():
        for q, fn in info.functions.items():
            by_name[fn.name].append((info.name, q))
            if getattr(fn, "_cls", None):
                methods[fn.name].append((info.name, q))
    module_level = {(info.name, q) for info in mods.values() for q in info.functions if "." not in q}

    def resolve(info, fn, call_func):
        cls = getattr(fn, "_cls", None)
        if isinstance(call_func, ast.Name):
            n = call_func.id
            # nested function of an enclosing function
            q = fn._qual
            parts = q.split(".")
            for i in range(len(parts), 0, -1):
                cand = ".".join(parts[:i]) + "." + n
                if cand in info.functions:
                    return [(info.name, cand)], "local"
            if n in info.functions:
                return [(info.name, n)], "module"
            if n in info.symbols:
                mod, name = info.symbols[n]
                if (mod, name) in module_level:
                    return [(mod, name)], "import"
                if mod in mods and name in mods[mod].classes:
                    return [], ""
            return [], ""
        if isinstance(call_func, ast.Attribute):
            attr = call_func.attr
            base = call_func.value
            if isinstance(base, ast.Name):
                if base.id in {"self", "cls"} and cls:
                    cand = f"{cls}.{attr}"
                    for q2 in info.functions:
                        if q2.endswith(cand) and q2.count(".") == cand.count("."):
                            return [(info.name, q2)], "self"
                alias = info.module_aliases.get(base.id)
                if alias and alias in mods and attr in mods[alias].functions:
                    return [(alias, attr)], "module-attr"
                if base.id in info.symbols:
                    mod, name = info.symbols[base.id]
                    if mod in mods and name in mods[mod].classes and f"{name}.{attr}" in mods[mod].functions:
                        return [(mod, f"{name}.{attr}")], "class-attr"
                if base.id in info.classes and f"{base.id}.{attr}" in info.functions:
                    return [(info.name, f"{base.id}.{attr}")], "class-attr"
            if attr.startswith("__"):
                return [], ""
            cands = methods.get(attr, [])
            if len(cands) == 1 and len(by_name.get(attr, [])) == 1:
                return cands, "by-name"
            return [], ""
        return [], ""

    edges = defaultdict(set)  # caller -> {(callee, how)}
    for info in mods.values():
        for q, fn in info.functions.items():
            me = (info.name, q)
            for node in own_nodes(fn):
                if isinstance(node, ast.Call):
                    targets, how = resolve(info, fn, node.func)
                    for t in targets:
                        edges[me].add((t, "call:" + how))
                    for a in [*node.args, *[k.value for k in node.keywords]]:
                        if isinstance(a, (ast.Name, ast.Attribute)):
                            targets, how = resolve(info, fn, a)
                            for t in targets:
                                edges[me].add((t, "ref:" + how))
                elif isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                    targets, how = resolve(info, fn, node.value)
                    for t in targets:
                        edges[me].add((t, "ref:" + how))
        # module-level references (callback registration at import time, decorators)
        for node in info.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    for a in [*sub.args, *[k.value for k in sub.keywords]]:
                        if isinstance(a, ast.Name) and a.id in info.functions:
                            edges[(info.name, "<module>")].add(((info.name, a.id), "ref:module"))
    rev = defaultdict(set)
    for caller, cs in edges.items():
        for callee, how in cs:
            rev[callee].add((caller, how))
    return edges, rev


def entry_label(mods, key):
    mod, q = key
    if q == "<module>":
        return f"module-level ref in {mod}"
    info = mods.get(mod)
    fn = info.functions.get(q) if info else None
    if fn is None:
        return None
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Name):
            router = dec.func.value.id
            if router in info.router_prefix or router in {"router", "app"}:
                prefix = info.router_prefix.get(router, "")
                if prefix == "<telegram>":
                    return f"telegram {dec.func.attr} {q}"
                path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else ""
                return f"route {dec.func.attr.upper()} {prefix}{path}"
    return None


def chains(mods, rev, start, max_depth=10, max_paths=6):
    """Paths from start upward to entry points or roots, BFS, shortest first."""
    out, roots, seen = [], [], {start}
    queue = deque([(start, [start])])
    while queue and len(out) + len(roots) < max_paths * 3:
        node, path = queue.popleft()
        label = entry_label(mods, node)
        callers = rev.get(node, set())
        if label and node != start:
            out.append((label, path))
            continue
        if label and node == start:
            out.append((label, path))
        if not callers:
            if node != start or not label:
                roots.append(path)
            continue
        if len(path) > max_depth:
            roots.append(path + [("…", "depth")])
            continue
        for caller, _how in sorted(callers):
            if caller in seen:
                continue
            seen.add(caller)
            queue.append((caller, path + [caller]))
    fmt = lambda p: " ← ".join(f"{m.rsplit('.', 1)[-1]}.{q}" if q != "depth" else "…" for m, q in p)  # noqa: E731
    return [f"{lbl}: {fmt(p)}" for lbl, p in out[:max_paths]], [fmt(p) for p in roots[:max_paths]]


# ---------------------------------------------------------------- main


def inventory(app: Path, classify: dict | None = None, *, paths: bool = True):
    """Scan ``app``: every site, the writer functions found directly, the transitive ones and the parsed modules.

    The module's model tables are rebuilt on every call, so two calls in one process are independent.
    ``paths=False`` skips the entry-path search (the report's ``entry_paths`` / ``roots``), which the guard
    does not read and which takes most of the time; detection is the same.
    """
    paths_wanted = paths
    MODELS_ALL.clear()
    _MODEL_NAMES.clear()
    MODELS_ALL.update(parse_models(app))
    mods = index_modules(app)
    direct = scan(mods)
    edges, rev = build_graph(mods)

    # writer helpers: functions with a direct certain site, closed under callers? no -- under "is called by":
    # a helper is a function whose body writes (directly or through another helper). Its call sites are sites.
    confirmed = set((classify or {}).get("_confirmed_candidate_writers", []))
    writer_funcs = {
        (info.name, q)
        for info, q, _n, kind, _m, _c, conf, _r in direct
        if not kind.startswith("lock-") and (conf == "certain" or f"{info.rel}::{q}" in confirmed)
    } | {(info.name, q) for info in mods.values() for q in info.functions if q.split(".")[-1] in SEED_HELPERS}
    transitive = set(writer_funcs)
    changed = True
    while changed:
        changed = False
        for caller, cs in edges.items():
            if caller in transitive or caller[1] == "<module>":
                continue
            if any(c in transitive and how.startswith("call") for c, how in cs):
                transitive.add(caller)
                changed = True

    sites: list[Site] = []
    counter = 0

    def add(info, q, node, kind, model, cols, conf, recv="", helper=""):
        nonlocal counter
        counter += 1
        fn = info.functions[q]
        par = parents_of(fn)
        scopes, opens, commits = func_facts(fn)
        key = (info.name, q)
        callers = sorted({f"{m.rsplit('.', 1)[-1]}.{c} ({how})" for (m, c), how in rev.get(key, set())})
        paths, roots = chains(mods, rev, key) if paths_wanted else ([], [])
        sites.append(
            Site(
                id="",
                file=info.rel,
                line=node.lineno,
                func=q,
                kind=kind,
                model=model,
                columns=cols,
                confidence=conf,
                receiver=recv,
                helper=helper,
                lexical_scopes=lexical_scopes(node, par, fn),
                after_commit_in_scope=after_commit_in_lane_scope(node, par, fn),
                func_scopes=scopes,
                func_opens_session=opens,
                func_commits=commits,
                callers=callers[:30],
                entry_paths=paths,
                roots=roots,
            )
        )

    for info, q, node, kind, model, cols, conf, recv in direct:
        add(info, q, node, kind, model, cols, conf, recv)
    direct_keys = {(s.file, s.line, s.func) for s in sites}
    # helper call sites
    for info in mods.values():
        for q, fn in info.functions.items():
            for node in own_nodes(fn):
                if not isinstance(node, ast.Call):
                    continue
                # resolve via the graph edges of this function
                targets = sorted(c for c, how in edges.get((info.name, q), set()) if how.startswith("call"))
                name = _call_name(node)
                hit = [t for t in targets if t in writer_funcs and t[1].split(".")[-1] == name]
                if not hit:
                    continue
                if (info.rel, node.lineno, q) in direct_keys and name in {"update", "delete", "insert"}:
                    continue
                helper = f"{hit[0][0].rsplit('.', 1)[-1]}.{hit[0][1]}"
                add(info, q, node, "helper-call", "", [], "certain", helper=helper)

    sites.sort(key=lambda s: (s.file, s.line, s.kind, s.model, tuple(s.columns), s.func, s.helper))
    for i, s in enumerate(sites, 1):
        s.id = f"S{i:04d}"
    return sites, writer_funcs, transitive, mods


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    app = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    classify = None
    if "--classify" in sys.argv:
        classify = json.loads(Path(sys.argv[sys.argv.index("--classify") + 1]).read_text(encoding="utf-8"))
    sites, writer_funcs, transitive, _mods = inventory(app, classify)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "writer_inventory.json").write_text(
        json.dumps(
            {
                "root": str(app),
                "targets": TARGETS,
                "seed_helpers": sorted(SEED_HELPERS),
                "writer_functions": sorted(f"{m}:{q}" for m, q in writer_funcs),
                "transitive_writer_functions": sorted(f"{m}:{q}" for m, q in transitive),
                "sites": [asdict(s) for s in sites],
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    write_md(out_dir, sites, writer_funcs, classify)


def _fkey(s: Site) -> str:
    return f"{s.file}::{s.func}"


def write_md(out_dir, sites, writer_funcs, classify):
    from collections import Counter

    lines = ["# G03 B r5 — writer inventory (generated)", ""]
    lines.append(
        "Generated by `writer_inventory.py` over `wt-g03b/backend/app` (read-only `ast` scan). Do not edit by hand;"
        " the classification is `classification.json`, merged by `--classify`."
    )
    lines.append("")
    by_kind = Counter(s.kind for s in sites)
    by_model = Counter((s.model or "helper") for s in sites)
    by_conf = Counter(s.confidence for s in sites)
    funcs = sorted({_fkey(s) for s in sites})
    lines.append(
        f"**Sites:** {len(sites)} in {len(funcs)} functions. Writer functions (direct or transitive): {len(writer_funcs)}."
    )
    lines.append("")
    lines.append("| by kind | n |\n|---|---|")
    lines += [f"| {k} | {v} |" for k, v in sorted(by_kind.items())]
    lines.append("")
    lines.append("| by model | n |\n|---|---|")
    lines += [f"| {k} | {v} |" for k, v in sorted(by_model.items())]
    lines.append("")
    late = [s for s in sites if s.after_commit_in_scope]
    lines.append(
        f"**Sites after a commit inside their lane scope** (the PostgreSQL guard ended at that commit): {len(late)}"
    )
    lines += [f"- {s.id} `{s.file}:{s.line}` `{s.func}` {s.kind} {s.helper or s.model}" for s in late]
    lines.append("")
    lines.append("| by confidence | n |\n|---|---|")
    lines += [f"| {k} | {v} |" for k, v in sorted(by_conf.items())]
    lines.append("")
    unclassified = []
    if classify is not None:
        lines.append("## Classified functions")
        lines.append("")
        lines.append("| # | Function | Sites | Kinds / models | Lock class | Transaction owner | Rule (spec) | r3 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        groups = defaultdict(list)
        for s in sites:
            groups[_fkey(s)].append(s)
        for n, key in enumerate(funcs, 1):  # noqa: B007
            group = groups[key]
            c = classify.get(key)
            if c is None:
                unclassified.append(key)
                c = {"class": "**UNCLASSIFIED**", "owner": "", "rule": "", "r3": ""}
            ids = ", ".join(s.id for s in group)
            kinds = ", ".join(
                sorted(
                    {
                        f"{s.kind}:{s.model or s.helper}"
                        + (f"[{','.join(s.columns)[:60]}]" if s.columns else "")
                        + ("?" if s.confidence == "candidate" else "")
                        for s in group
                    }
                )
            )
            lines.append(
                f"| {n} | `{key}` | {ids} | {kinds} | {c['class']} | {c['owner']} | {c['rule']} | {c.get('r3', '')} |"
            )
        lines.append("")
        lines.append(f"**Unclassified functions:** {len(unclassified)}")
        for u in unclassified:
            lines.append(f"- `{u}`")
        stale = sorted(k for k in set(classify) - set(funcs) if not k.startswith("_"))
        lines.append("")
        lines.append(f"**Classification entries naming no inventory function (stale):** {len(stale)}")
        for u in stale:
            lines.append(f"- `{u}`")
        lines.append("")
    lines.append("## Sites")
    lines.append("")
    for s in sites:
        lines.append(f"### {s.id} · `{s.file}:{s.line}` · `{s.func}`")
        lines.append(
            f"- kind `{s.kind}`, model `{s.model or '-'}`, columns `{', '.join(s.columns) or '-'}`, {s.confidence}"
            + (f", receiver `{s.receiver}`" if s.receiver else "")
            + (f", helper `{s.helper}`" if s.helper else "")
        )
        lines.append(
            f"- lexical scopes: {'; '.join(f'`{x}`' for x in s.lexical_scopes) or 'none'}; function scopes: "
            f"{', '.join(s.func_scopes) or 'none'}; opens session: {s.func_opens_session}; commits: {s.func_commits}"
            + ("; **after a commit inside its lane scope**" if s.after_commit_in_scope else "")
        )
        if s.callers:
            lines.append(f"- callers: {'; '.join(s.callers[:12])}" + (" …" if len(s.callers) > 12 else ""))
        for p in s.entry_paths:
            lines.append(f"- entry: {p}")
        for p in s.roots[:3]:
            lines.append(f"- root: {p}")
        lines.append("")
    (out_dir / "writer_inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"sites={len(sites)} functions={len(funcs)}")
    if classify is not None:
        stale = sorted(k for k in set(classify) - set(funcs) if not k.startswith("_"))
        print(f"unclassified={len(unclassified)} stale={len(stale)}")
        if unclassified or stale:
            sys.exit(1)


if __name__ == "__main__":
    main()
