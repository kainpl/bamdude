"""Every write of the Spoolman mode names the new mode to its change of the source (C047 A09).

The slot ledgers hold the active mode only. ``SourceChange.decide(enabled=…)`` waits for the
slot-ledger writers and empties the ledger the mode leaves; a door that writes
``spoolman_enabled`` — or deletes its row, which then reads as the default — without naming
the mode there leaves the other mode's rows behind, to be taken for the active mode's by
every reader. The reset did (A09). A door that takes the credential anchor before ``decide``
waits for the writers before it (``wait_before_anchor``, A12).

The scan reads the source under ``backend/app`` (not ``migrations/`` or ``models/``) and finds
each function that writes a ``settings`` row whose key is ``spoolman_enabled`` or not known:

- a call of a keyed helper (KEYED_HELPERS) whose key, or dict of keys, is not a string the
  scan can read (see below), positional or keyword; through ``functools.partial`` too, where
  only a key bound by position counts (the caller can replace one bound by keyword); a helper
  handed on as a value (acceptance A15, A17, A19). The helper is found by its name, an import
  alias (a relative import too), a module path (``cloud_credentials.write``,
  ``backend.app.services.cloud_credentials.write``, a package's ``services.cloud_credentials``),
  a star import of its module, or a name bound to it or to its module; a name bound to two
  helpers has no readable key;
- the ``Settings`` constructor (any alias) with such a key, or with no readable key;
- a SQLAlchemy ``delete`` / ``update`` / ``insert`` (or a name bound to one) of the model or its
  table — ``Settings``, ``Settings.__table__``, a name bound to either,
  ``metadata.tables["settings"]`` — a dialect's insert through its module too; ``.delete()`` /
  ``.update()`` / ``.insert()`` called on the table; a legacy ``query(Settings)…`` bulk
  ``.delete()`` / ``.update(…)``; a bulk ``*_mappings`` / ``bulk_save_objects`` of the model; a
  lightweight ``Table("settings", …)``;
- SQL text that writes the table, its comments aside, with or without a schema:
  ``INSERT [OR …] INTO``, ``REPLACE INTO``, ``MERGE INTO``, ``UPDATE [OR …]``, ``DELETE FROM``,
  ``TRUNCATE`` and ``DROP TABLE`` (the table anywhere in their list), ``ALTER TABLE``, ``COPY``;
- in a function that names the model: any ``.delete(…)`` (an ORM row, a query, a table bound
  any way), a ``.update()`` / ``.insert()`` without arguments, a ``row.value =``, a
  ``setattr(row, "value", …)``.

A name is bound to a helper, a module, a statement, the model or its table by ``=`` (element by
element into a tuple or list), an annotated assignment or ``:=``, anywhere in the module. A
string the scan can read is a literal, a constant of the module or one imported (by name or
through its module), read the way Python binds the name where it is read: a name the
function, or one around it, binds itself (a parameter, an assignment, a loop, ``with``,
``except``, a comprehension, a lambda's parameter, a ``match`` capture, ``global`` /
``nonlocal``) is its own, and its own import is read through its module. A module-level name
is a constant only when every binding of it at module level is a top-level ``NAME = "…"``, and
an import only when every one is an import of the same thing; any other binding — an assignment
after the import, one in a block, a loop, a ``match`` capture, a parameter of a module-level
lambda, ``del``, ``global`` in a function — or a star import in the module leaves it unread.

Each is a MODE writer, whose door names the mode, a keyed helper (its callers are scanned), or
is listed in NOT_THE_MODE with the keys it writes; a new writer fails here until it is
classified. SEEN pins the forms above and NOT_SEEN the resolution. The scan cannot prove that
no other form exists; BLIND pins examples of what it cannot see: the model, its table or a row
carried through variables into a function that does not name the model (a parameter, a
helper's return value), SQL or a table name assembled at run time (an f-string, a sum of
strings), a helper reached through a name built at run time (``getattr``). The whole-database
restore is outside the scan that way: it replaces the database below it — on SQLite the file,
through the backup API; on PostgreSQL every table, by names read at run time — and asks for a
restart.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
MODEL_MODULE = "backend.app.models.settings"
_NAME = r"(?:[\"`\[]?\w+[\"`\]]?\.)?[\"`\[]?\w+[\"`\]]?"  # a table, with or without a schema
_SETTINGS = r"(?:[\"`\[]?\w+[\"`\]]?\.)?[\"`\[]?settings[\"`\]]?\b"
_CONFLICT = r"(?:OR\s+(?:ROLLBACK|ABORT|REPLACE|FAIL|IGNORE)\s+)?"  # SQLite's ``INSERT OR …`` / ``UPDATE OR …``
SQL_WRITE = re.compile(
    rf"\b(?:INSERT\s+{_CONFLICT}INTO\s+|REPLACE\s+INTO\s+|MERGE\s+INTO\s+|UPDATE\s+{_CONFLICT}(?:ONLY\s+)?"
    r"|DELETE\s+FROM\s+(?:ONLY\s+)?|ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?|COPY\s+"
    rf"|(?:TRUNCATE(?:\s+TABLE)?|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+(?:(?:ONLY\s+)?{_NAME}(?:\s*\*)?\s*,\s*)*"
    rf"(?:ONLY\s+)?){_SETTINGS}",
    re.IGNORECASE,
)
SQL_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)
STATEMENTS = {"delete", "update", "insert"}
BULK = {"bulk_insert_mappings", "bulk_update_mappings", "bulk_save_objects"}
AMBIGUOUS = "<a name bound to two keyed helpers>"

# A helper whose key comes from its caller: name → (its definition, the key's position in a
# call and its keyword, one key or a dict of keys, the module a bare name must come from — None
# when the name is the helper's wherever it is called).
KEYED_HELPERS = {
    "set_setting": (("api/routes/settings.py", "set_setting"), 1, "key", "key", None),
    "upsert_setting": (("core/db_dialect.py", "upsert_setting"), 2, "key", "key", None),
    "put_record": (("core/schema_repair_state.py", "put_record"), 1, "key", "key", None),
    "_write_setting": (("services/library_trash.py", "LibraryTrashService._write_setting"), 1, "key", "key", None),
    "write": (
        ("services/cloud_credentials.py", "write"),
        2,
        "values",
        "dict",
        "backend.app.services.cloud_credentials",
    ),
    "_write_credentials": (("api/routes/orca_cloud.py", "_write_credentials"), 2, "values", "dict", None),
}

# A writer of the mode → the door that owns its change: (file, function) whose ``decide`` names
# ``enabled=``, and whether that door takes the anchor first (then it calls ``wait_before_anchor``).
MODE_WRITERS = {
    ("api/routes/settings.py", "_write_spoolman_settings"): (
        ("api/routes/settings.py", "update_spoolman_settings"),
        (),
    ),
    ("api/routes/settings.py", "_update_settings"): (
        ("api/routes/settings.py", "_update_settings"),
        (("api/routes/settings.py", "_update_settings"),),
    ),
    ("api/routes/settings.py", "_reset_settings"): (
        ("api/routes/settings.py", "_reset_settings"),
        (("api/routes/settings.py", "reset_settings"),),
    ),
    ("services/git_restore.py", "GitRestoreService._restore_settings"): (
        ("services/git_restore.py", "GitRestoreService._restore_settings"),
        (("services/git_restore.py", "GitRestoreService._apply"),),
    ),
}

# Writers whose keys are never the mode, and the keys they write.
NOT_THE_MODE = {
    ("api/routes/mfa.py", "_set_email_2fa_enabled"): "user_<id>_email_2fa_enabled",
    ("api/routes/model_sources.py", "save_connection"): "the model-source connection keys",
    ("api/routes/orca_cloud.py", "_persist_rotated_tokens"): "the Orca Cloud token keys",
    ("api/routes/support.py", "_set_debug_setting"): "debug_logging_enabled, debug_logging_enabled_at",
    ("cli.py", "_reset_admin"): "setup_completed, auth_enabled",
    ("cli.py", "_reset_password"): "user_<id>_email_2fa_enabled",
    ("core/credential_config.py", "initialize_credential_config_anchor"): "the credential anchor row",
    ("core/credential_config.py", "lock_credential_config"): "the credential anchor row",
    ("core/db_portable.py", "_export_pg_to_sqlite"): "the portable-backup marker, in the exported copy",
    ("core/schema_repair_state.py", "claim_attempt"): "the private import-repair record",
    ("core/schema_repair_state.py", "fail_attempt"): "the private import-repair record",
    ("core/schema_repair_state.py", "finish_attempt"): "the private import-repair record",
    ("core/schema_repair_state.py", "write_pending"): "the private import-repair record and its audit",
    ("main.py", "_application_lifespan._reconcile_locale_on_startup"): "notification_language",
    ("main.py", "_save_ams_drying_latch"): "the AMS drying latch",
    ("services/archive_cleanup_service.py", "ArchiveCleanupService._stamp_last_run"): "the cleanup's last-run stamps",
    ("services/archive_purge.py", "ArchivePurgeService.set_retention_days"): "the archive trash retention",
    ("services/bambu_cloud_credentials.py", "_persist_renewed"): "the Bambu Cloud token keys",
    ("services/bambu_cloud_credentials.py", "mark_cloud_token_invalid"): "the Bambu Cloud token keys",
    ("services/connector_submissions.py", "server_identity"): "the connector server id and storage epoch",
    ("services/connector_submissions.py", "stage_storage_epoch"): "the connector storage epoch",
    ("services/email_service.py", "save_smtp_settings"): "the SMTP keys",
    ("services/library_trash.py", "LibraryTrashService.set_retention_days"): "the library trash retention",
    ("services/obico_detection.py", "drop_printer_from_watch_list"): "obico_enabled_printers",
    ("services/spoolman_usage_history.py", "_stage_source"): "the history-source record, never the mode",
    ("services/user_content_delete.py", "detach_account_rows"): "user_<id>_email_2fa_enabled",
}


def _modules():
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith(("migrations/", "models/")):
            continue
        yield rel, path.read_text(encoding="utf-8")


def _rel(module: str | None) -> str | None:
    """The file of a ``backend.app`` module (a package's ``__init__`` is not resolved)."""
    if not module or not module.startswith("backend.app."):
        return None
    return module.removeprefix("backend.app.").replace(".", "/") + ".py"


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _module_level(tree):
    """The module's own nodes: a function, class or lambda is yielded, its body is not."""
    stack = list(ast.iter_child_nodes(tree))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(node))


def _fold(node, local: dict[str, str]) -> str | None:
    """A string built at import time from literals and earlier constants of the module."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return local.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left, local), _fold(node.right, local)
        return left + right if left is not None and right is not None else None
    return None


def _absolute(rel: str, node: ast.ImportFrom) -> str | None:
    """The module a from-import names; a relative one is resolved against its file's package."""
    if not node.level:
        return node.module
    if not rel:
        return None
    package = f"backend/app/{rel}".removesuffix(".py").split("/")[: -node.level]
    return ".".join([*package, node.module] if node.module else package)


def _import_bindings(rel: str, node) -> list[tuple[str, str | None]]:
    """(local name, the dotted path it is bound to) for each name an import binds; ``*`` for a star."""
    if isinstance(node, ast.ImportFrom):
        module = _absolute(rel, node)
        return [
            ("*", module) if alias.name == "*" else (alias.asname or alias.name, module and f"{module}.{alias.name}")
            for alias in node.names
        ]
    return [
        (alias.asname, alias.name) if alias.asname else (alias.name.split(".")[0], alias.name.split(".")[0])
        for alias in node.names
    ]


def _pairs(target, value):
    """(name, value) for each name a target binds to a part of the value it is given."""
    if isinstance(target, ast.Name):
        yield target.id, value
    elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        if any(isinstance(item, ast.Starred) for item in value.elts):
            return
        stars = [index for index, item in enumerate(target.elts) if isinstance(item, ast.Starred)]
        head = stars[0] if stars else len(target.elts)
        tail = len(target.elts) - head - 1 if stars else 0
        if (len(value.elts) < head + tail) if stars else (len(value.elts) != head):
            return
        for item, part in zip(target.elts[:head], value.elts[:head], strict=True):
            yield from _pairs(item, part)
        for item, part in zip(
            target.elts[len(target.elts) - tail :], value.elts[len(value.elts) - tail :], strict=True
        ):
            yield from _pairs(item, part)


_BINDINGS = (ast.Assign, ast.AnnAssign, ast.NamedExpr)


def _bound_pairs(node):
    """(name, value) for what a node binds a name to: ``=`` (element by element into a tuple or
    list), an annotated assignment, ``:=``."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _pairs(target, node.value)
    elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
        yield from _pairs(node.target, node.value)


def _statements(node):
    """Every statement under a node, without walking into expressions."""
    for field in ("body", "orelse", "finalbody", "handlers", "cases"):
        for child in getattr(node, field, None) or ():
            if isinstance(child, ast.stmt):
                yield child
            yield from _statements(child)


@functools.cache
def _module_bindings(rel: str, tree) -> dict[str, set[tuple]]:
    """How the module binds each name at its own level (acceptance A19).

    ``("constant",)`` — a top-level ``NAME = …`` / ``NAME: T = …``; ``("import", path)`` — an
    import and what it binds; ``("other",)`` — any other binding: one in a block, a loop, a
    definition, ``except``, a ``match`` capture, ``del``, ``global`` in a function, a name a
    module-level lambda binds. A star import is the name ``*``.
    """
    bindings: dict[str, set[tuple]] = {}

    def bind(name, kind):
        bindings.setdefault(name, set()).add(kind)

    top: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            top.update(id(target) for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            top.add(id(node.target))
    for node in _statements(tree):
        if isinstance(node, ast.Global):
            for name in node.names:
                bind(name, ("other",))
    for node in _module_level(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bind(node.id, ("constant",) if id(node) in top else ("other",))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for name, path in _import_bindings(rel, node):
                bind(name, ("import", path))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or (
            isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name
        ):
            bind(node.name, ("other",))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(node.rest, ("other",))
        elif isinstance(node, ast.Lambda):
            for inner in ast.walk(node):
                if isinstance(inner, ast.arg):
                    bind(inner.arg, ("other",))
                elif isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                    bind(inner.id, ("other",))
    return bindings


def _constants_of(rel: str, tree) -> dict[tuple[str, str], str]:
    """(file, name) → the string a module-level ``NAME = "…"`` (or a sum of such) binds.

    Only a name every module-level binding of which is such an assignment is a constant
    (``_module_bindings``); a module with a star import has none.
    """
    bindings = _module_bindings(rel, tree)
    if "*" in bindings:
        return {}
    stable = {name for name, kinds in bindings.items() if kinds == {("constant",)}}
    local: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        value = _fold(node.value, {name: text for name, text in local.items() if name in stable})
        for target in targets:
            if isinstance(target, ast.Name):
                if value is None:
                    local.pop(target.id, None)
                else:
                    local[target.id] = value
    return {(rel, name): value for name, value in local.items() if name in stable}


@functools.cache
def _trees():
    return tuple((rel, ast.parse(source)) for rel, source in _modules())


@functools.cache
def _module_constants() -> dict[tuple[str, str], str]:
    constants = {}
    for rel, tree in _trees():
        constants.update(_constants_of(rel, tree))
    return constants


def _own_view(nodes, rel: str = "") -> dict[str, tuple]:
    """How one function binds names itself: ``("import", (file, name) or None, path)`` or ``("local",)``.

    A name both imported and bound another way in the same function, or imported twice to
    different things, is a local.
    """
    imports: dict[str, tuple] = {}
    local: set[str] = set()
    for node in nodes:
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            for name, path in _import_bindings(rel, node):
                if path is None:
                    local.add(name)
                    continue
                file = _rel(path.rsplit(".", 1)[0]) if isinstance(node, ast.ImportFrom) else None
                binding = ("import", (file, path.rsplit(".", 1)[1]) if file else None, path)
                if imports.setdefault(name, binding) != binding:
                    local.add(name)
        elif isinstance(node, ast.arg):
            local.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            local.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            local.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            local.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            local.add(node.rest)
    view = {name: binding for name, binding in imports.items() if name not in local}
    view.update(dict.fromkeys(local, ("local",)))
    return view


def _root(node):
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _Names:
    """What one module's names are bound to by its imports and assignments."""

    def __init__(self, rel: str, tree: ast.AST, constants: dict):
        self.rel, self.constants = rel, constants
        bindings = _module_bindings(rel, tree)
        star = "*" in bindings
        imports, pairs, own = [], [], False
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                imports.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "Settings":
                own = True
            if isinstance(node, _BINDINGS):
                # Only a name, an attribute or a subscript can be a helper, the model, a module or a statement.
                pairs.extend(
                    pair for pair in _bound_pairs(node) if isinstance(pair[1], (ast.Name, ast.Attribute, ast.Subscript))
                )
        # The model's name unless the module has a class of its own by that name (core/config.py).
        self.models: set[str] = set() if own else {"Settings"}
        self.statements = {name: name for name in STATEMENTS}  # local name → delete / update / insert
        self.sqlalchemy_names: set[str] = set()  # local names of sqlalchemy modules and their members
        self.helpers: dict[str, set[str]] = {}  # local name → every keyed helper it may be
        # Imports and assignments anywhere in the module say what a name may be (a helper, the
        # model, a module); only a module-level import that is the name's one binding at module
        # level resolves a constant -- a function's own imports are in its view (acceptance A19).
        self.modules_anywhere: dict[str, set[str]] = {}  # local name → every dotted path it may be
        self.modules: dict[str, str] = {}  # local name → the dotted path its one import binds
        self.imported: dict[str, tuple[str, str]] = {}  # local name → (file, name) of that import
        module_level = {id(node) for node in _module_level(tree) if isinstance(node, (ast.ImportFrom, ast.Import))}
        for node in imports:
            module = _absolute(rel, node) if isinstance(node, ast.ImportFrom) else None
            for (local, path), alias in zip(_import_bindings(rel, node), node.names, strict=True):
                if local == "*":
                    # A star import may bind any keyed helper of its module.
                    for name, spec in KEYED_HELPERS.items():
                        if spec[4] in {None, module}:
                            self.helpers.setdefault(name, set()).add(name)
                    continue
                if isinstance(node, ast.ImportFrom):
                    if module in {MODEL_MODULE, "backend.app.models"} and alias.name == "Settings":
                        self.models.add(local)
                    if (module or "").startswith("sqlalchemy"):
                        if alias.name in STATEMENTS:
                            self.statements[local] = alias.name
                        else:
                            self.sqlalchemy_names.add(local)
                    spec = KEYED_HELPERS.get(alias.name)
                    if spec and spec[4] in {None, module}:
                        self.helpers.setdefault(local, set()).add(alias.name)
                elif alias.name.split(".")[0] == "sqlalchemy":
                    self.sqlalchemy_names.add(local)
                if path is None:
                    continue
                self.modules_anywhere.setdefault(local, set()).add(path)
                if id(node) in module_level and not star and bindings.get(local) == {("import", path)}:
                    self.modules[local] = path
                    file = _rel(module) if isinstance(node, ast.ImportFrom) else None
                    if file is not None:
                        self.imported[local] = (file, alias.name)
        changed = True
        while changed:
            # ``put = set_setting``, ``t = Settings.__table__``, ``cc = cloud_credentials``,
            # ``op = sa.delete``: a name bound to a helper, the model or its table, a module or a
            # statement may be that, wherever in the module it is bound.
            changed = False
            for target, value in pairs:
                if self.is_model(value) and target not in self.models:
                    self.models.add(target)
                    changed = True
                statement = self.statement_ref(value)
                if statement is not None and target not in self.statements:
                    self.statements[target] = statement
                    changed = True
                if _root(value) in self.sqlalchemy_names and target not in self.sqlalchemy_names:
                    self.sqlalchemy_names.add(target)
                    changed = True
                for mapping, found in (
                    (self.helpers, self.alias_of_helper(value)),
                    (self.modules_anywhere, self._paths_anywhere(value)),
                ):
                    if not found <= mapping.get(target, set()):
                        mapping.setdefault(target, set()).update(found)
                        changed = True

    def literal(self, node, view=None) -> str | None:
        view = view or {}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            binding = view.get(node.id)
            if binding is not None:
                # A name the function binds is its own, never the module's constant (acceptance A17).
                return self.constants.get(binding[1]) if binding[0] == "import" and binding[1] else None
            if (self.rel, node.id) in self.constants:
                return self.constants[(self.rel, node.id)]
            if node.id in self.imported:
                return self.constants.get(self.imported[node.id])
            return None
        if isinstance(node, ast.Attribute):
            module = self.path(node.value, view)
            if module and _rel(module) is not None:
                return self.constants.get((_rel(module), node.attr))
        return None

    def may_be_the_mode(self, node, view=None) -> bool:
        key = self.literal(node, view)
        return key is None or key == "spoolman_enabled"

    def path(self, node, view=None) -> str | None:
        """The dotted path a name or an attribute chain names, by the scope's imports or the module's one."""
        view = view or {}
        attrs = []
        while isinstance(node, ast.Attribute):
            attrs.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        binding = view.get(node.id)
        base = (binding[2] if binding[0] == "import" else None) if binding else self.modules.get(node.id)
        return ".".join([base, *reversed(attrs)]) if base else None

    def _paths_anywhere(self, node) -> set[str]:
        """Every dotted path a name or an attribute chain may name, by what binds it anywhere."""
        attrs = []
        while isinstance(node, ast.Attribute):
            attrs.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return set()
        return {".".join([base, *reversed(attrs)]) for base in self.modules_anywhere.get(node.id, ())}

    def is_model(self, node, view=None) -> bool:
        """The model, its table, or a name bound to either."""
        if isinstance(node, ast.Name):
            return node.id in self.models
        if isinstance(node, ast.Attribute) and node.attr == "__table__":
            return self.is_model(node.value, view)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "tables":
            return self.literal(node.slice, view) == "settings"  # ``metadata.tables["settings"]``
        return isinstance(node, ast.Attribute) and node.attr == "Settings"

    def statement_ref(self, func) -> str | None:
        if isinstance(func, ast.Name):
            return self.statements.get(func.id)
        if isinstance(func, ast.Attribute) and func.attr in STATEMENTS and _root(func.value) in self.sqlalchemy_names:
            return func.attr  # ``sa.delete``, ``postgresql.insert``, ``sa.dialects.sqlite.insert``
        return None

    def statement(self, call: ast.Call) -> str | None:
        return self.statement_ref(call.func)

    def helper_refs(self, node) -> set[str]:
        """Every keyed helper a name or an attribute may refer to."""
        if isinstance(node, ast.Name):
            refs = set(self.helpers.get(node.id, ()))
            spec = KEYED_HELPERS.get(node.id)
            if spec and spec[0][0] == self.rel and spec[0][1] == node.id:
                refs.add(node.id)  # in its own module
            return refs
        if isinstance(node, ast.Attribute):
            spec = KEYED_HELPERS.get(node.attr)
            if spec and (spec[4] is None or spec[4] in self._paths_anywhere(node.value)):
                return {node.attr}
        return set()

    def helper_ref(self, node) -> str | None:
        """The keyed helper a name or an attribute refers to; AMBIGUOUS when it may be two (A19)."""
        refs = self.helper_refs(node)
        if not refs:
            return None
        return next(iter(refs)) if len(refs) == 1 else AMBIGUOUS

    def alias_of_helper(self, node) -> set[str]:
        refs = self.helper_refs(node)
        if isinstance(node, ast.Name):
            spec = KEYED_HELPERS.get(node.id)
            if spec and spec[4] is None:
                refs.add(node.id)
        return refs

    def helper(self, call: ast.Call) -> str | None:
        return self.helper_ref(call.func)


def _scopes(tree, rel: str = ""):
    """(qualified name, nodes, view) of the module and every function or class.

    A nested function's nodes are its own; its view is how its names are bound — by itself
    and by the functions around it (a class body is not around its methods). The module's
    view is empty: its names are read through its constants and imports. ``rel`` resolves a
    function's relative import.
    """
    out = []

    def collect(owner, qual, outer):
        nodes, nested, stack = [], [], list(ast.iter_child_nodes(owner))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nested.append(node)
                continue
            nodes.append(node)
            stack.extend(ast.iter_child_nodes(node))
        if owner is tree:
            view, inner = {}, {}
        else:
            own = _own_view(nodes, rel)
            own.update(dict.fromkeys((node.name for node in nested), ("local",)))
            view = {**outer, **own}
            inner = outer if isinstance(owner, ast.ClassDef) else view
        out.append((qual or "<module>", nodes, view))
        for node in nested:
            collect(node, f"{qual}.{node.name}" if qual else node.name, inner)

    collect(tree, "", {})
    return out


def _called(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _argument(call: ast.Call, index: int, keyword: str):
    """The argument a helper takes its key from, or None when the call does not pass it readably."""
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    if any(isinstance(arg, ast.Starred) for arg in call.args[: index + 1]):
        return None
    return call.args[index] if len(call.args) > index else None


def _helper_keys(call: ast.Call, helper: str):
    """The key nodes a keyed-helper call writes; None when they cannot be read."""
    if helper not in KEYED_HELPERS:
        return None  # AMBIGUOUS: which argument is the key depends on which helper it is
    _definition, index, keyword, kind, _module = KEYED_HELPERS[helper]
    argument = _argument(call, index, keyword)
    if argument is None:
        return None
    if kind == "key":
        return [argument]
    if isinstance(argument, ast.Dict) and None not in argument.keys:
        return list(argument.keys)
    return None


def _replaceable_key(partial: ast.Call, helper: str) -> bool:
    """Whether a ``partial`` binds a helper's key by keyword (or ``**``), which its caller can replace."""
    if helper not in KEYED_HELPERS:
        return True  # AMBIGUOUS
    return any(item.arg in {None, KEYED_HELPERS[helper][2]} for item in partial.keywords)


def _a_method_write(call: ast.Call) -> bool:
    """A write, in a function that names the model: ``db.delete(row)``, a query's or a table's
    ``.delete()``, a ``.update()`` / ``.insert()`` without arguments (a table bound any way)."""
    func = call.func
    return isinstance(func, ast.Attribute) and (
        func.attr == "delete" or (func.attr in STATEMENTS and not call.args and not call.keywords)
    )


def _queries_the_model(names: _Names, node, view) -> bool:
    """Whether a method chain starts from a legacy ``….query(<the model>)``."""
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Call):
            if _called(node) == "query" and any(names.is_model(arg, view) for arg in node.args):
                return True
            node = node.func
        else:
            node = node.value
    return False


def _writes(names: _Names, nodes, view=None) -> bool:
    view = view or {}
    names_model = any(
        isinstance(n, (ast.Name, ast.Attribute, ast.Subscript)) and names.is_model(n, view) for n in nodes
    )
    # A helper's reference that is called, bound to another name or partially applied is read
    # where that happens; any other is the helper handed on as a value (acceptance A17).
    read = set()
    for node in nodes:
        if isinstance(node, ast.Call):
            read.add(id(node.func))
            if _called(node) == "partial" and node.args:
                read.add(id(node.args[0]))
        elif isinstance(node, _BINDINGS):
            read.update(id(value) for _name, value in _bound_pairs(node))
    for node in nodes:
        if (
            isinstance(node, (ast.Name, ast.Attribute))
            and isinstance(node.ctx, ast.Load)
            and id(node) not in read
            and names.helper_ref(node) is not None
        ):
            return True
        if isinstance(node, ast.Call):
            helper, call = names.helper(node), node
            if _called(node) == "partial" and node.args and names.helper_ref(node.args[0]):
                # ``partial(set_setting, db)``: the key is read from what the partial binds by
                # position; one bound by keyword the caller can replace (acceptance A19).
                helper = names.helper_ref(node.args[0])
                if _replaceable_key(node, helper):
                    return True
                call = ast.Call(func=node.args[0], args=node.args[1:], keywords=node.keywords)
            if helper is not None:
                keys = _helper_keys(call, helper)
                if keys is None or any(names.may_be_the_mode(key, view) for key in keys):
                    return True
            if names.is_model(node.func, view):
                key = next((k.value for k in node.keywords if k.arg == "key"), None)
                if key is None or names.may_be_the_mode(key, view):
                    return True
            if names.statement(node) and node.args and names.is_model(node.args[0], view):
                return True
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in STATEMENTS
                and names.is_model(node.func.value, view)
            ):
                return True  # ``Settings.__table__.delete()``, ``t.update()``, ``metadata.tables[…].insert()``
            if _called(node) in BULK and node.args and names.is_model(node.args[0], view):
                return True
            if _called(node) in {"Table", "table"} and node.args and names.literal(node.args[0], view) == "settings":
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"delete", "update"}:
                if _queries_the_model(names, node.func.value, view):
                    return True  # a legacy ``db.query(Settings)…`` bulk ``.delete()`` / ``.update(…)``
            if names_model and _a_method_write(node):
                return True
            if names_model and _called(node) == "setattr" and len(node.args) >= 2:
                if names.literal(node.args[1], view) in {"value", None}:
                    return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SQL_WRITE.search(SQL_COMMENT.sub(" ", node.value))
        ):
            return True
        if names_model and isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr == "value" for t in targets):
                return True
    return False


@functools.cache
def _parsed():
    constants = _module_constants()
    return tuple((rel, _Names(rel, tree, constants), _scopes(tree, rel)) for rel, tree in _trees())


def _found():
    return {
        (rel, qual) for rel, names, scopes in _parsed() for qual, nodes, view in scopes if _writes(names, nodes, view)
    }


def _function(rel: str, qual: str):
    source = (APP / rel).read_text(encoding="utf-8")
    node = ast.parse(source)
    for part in qual.split("."):
        node = next(
            (
                child
                for child in ast.iter_child_nodes(node)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == part
            ),
            None,
        )
        if node is None:
            raise AssertionError(f"{rel}: no function {qual}")
    return source, node


def _function_source(rel: str, qual: str) -> str:
    source, node = _function(rel, qual)
    return ast.get_source_segment(source, node) or ""


def test_every_writer_of_a_settings_row_that_may_be_the_mode_is_classified():
    found = _found()
    helpers = {definition for definition, *_rest in KEYED_HELPERS.values()}
    known = set(MODE_WRITERS) | set(NOT_THE_MODE)
    assert sorted(found - known - helpers) == [], "a new writer of a settings row: a MODE writer, or its keys"
    assert sorted(known - found) == [], "a listed writer is gone: drop it from the list"
    assert not set(MODE_WRITERS) & set(NOT_THE_MODE)
    assert not helpers & known


def test_a_keyed_helper_takes_its_key_where_the_scan_reads_it():
    for name, ((rel, qual), index, keyword, _kind, _module) in sorted(KEYED_HELPERS.items()):
        _source, node = _function(rel, qual)
        assert node.name == name
        parameters = [arg.arg for arg in node.args.posonlyargs + node.args.args]
        assert parameters[index] == keyword, f"{rel}::{qual} takes its key elsewhere"


_IMPORTS = """\
import functools
import sqlalchemy as sa
from functools import partial
from sqlalchemy import delete as sa_delete, insert, text, update
from sqlalchemy.dialects import postgresql
from backend.app.api.routes.settings import set_setting
from backend.app.core.db_dialect import upsert_setting
from backend.app.models.settings import Settings
from backend.app.models.settings import Settings as Row
from backend.app.services import cloud_credentials
from backend.app.services.cloud_credentials import write
put = set_setting
KEY = 'time_format'
"""

# The forms the docstring says the scan sees (acceptance A15 and A17: the reviewers' probes).
SEEN = {
    "a variable key": "async def f(db, key, v):\n    await set_setting(db, key, v)\n",
    "the mode by keyword": "async def f(db, v):\n    await set_setting(db, key='spoolman_enabled', value=v)\n",
    "a helper bound to a name": "async def f(db, key, v):\n    await put(db, key, v)\n",
    "upsert_setting": "async def f(db, key, v):\n    await upsert_setting(db, Settings, key, v)\n",
    "a dict of keys": "async def f(db, c, v):\n    await write(db, c, {'spoolman_enabled': v})\n",
    "a dict built elsewhere": "async def f(db, c, values):\n    await cloud_credentials.write(db, c, values)\n",
    "a constant shadowed by a parameter": "async def f(db, KEY, v):\n    await set_setting(db, KEY, v)\n",
    "a constant shadowed by a default": (
        "async def f(db, v, KEY='spoolman_enabled'):\n    await set_setting(db, KEY, v)\n"
    ),
    "a constant shadowed by a loop": (
        "async def f(db, keys, v):\n    for KEY in keys:\n        await set_setting(db, KEY, v)\n"
    ),
    "a constant shadowed by an assignment": "async def f(db, k, v):\n    KEY = k\n    await set_setting(db, KEY, v)\n",
    "a constant shadowed in the constructor": "def f(db, KEY, v):\n    db.add(Settings(key=KEY, value=v))\n",
    "a constant shadowed by the enclosing function": (
        "def f(KEY):\n    async def g(db, v):\n        await set_setting(db, KEY, v)\n    return g\n"
    ),
    "a constant rebound through global": (
        "def g(k):\n    global KEY\n    KEY = k\nasync def f(db, v):\n    await set_setting(db, KEY, v)\n"
    ),
    "functools.partial": "def f(db):\n    return functools.partial(set_setting, db)\n",
    "partial": "def f(db):\n    return partial(put, db)\n",
    "a helper handed on as a value": "def f(register):\n    register(set_setting)\n",
    "update(Settings)": "async def f(db):\n    await db.execute(update(Settings).values(value='x'))\n",
    "Settings.__table__.delete()": "async def f(db):\n    await db.execute(Settings.__table__.delete())\n",
    "Settings.__table__.update()": "async def f(db):\n    await db.execute(Settings.__table__.update().values(value='x'))\n",
    "insert(Settings.__table__)": "async def f(db):\n    await db.execute(insert(Settings.__table__).values(key='k'))\n",
    "a table bound to a name": "async def f(db):\n    t = Settings.__table__\n    await db.execute(t.delete())\n",
    "the metadata's table": "async def f(db, metadata):\n    await db.execute(metadata.tables['settings'].delete())\n",
    "a dialect insert through its module": (
        "async def f(db):\n    await db.execute(postgresql.insert(Settings).values(key='k'))\n"
    ),
    "an aliased delete": "async def f(db):\n    await db.execute(sa_delete(Settings))\n",
    "sa.delete": "async def f(db):\n    await db.execute(sa.delete(Row))\n",
    "INSERT INTO": "async def f(db):\n    await db.execute(text('INSERT INTO settings (key, value) VALUES (:k, :v)'))\n",
    "INSERT OR REPLACE": "async def f(db):\n    await db.execute(text('INSERT OR REPLACE INTO settings VALUES (:k, :v)'))\n",
    "REPLACE INTO": "async def f(db):\n    await db.execute(text('REPLACE INTO \"settings\" VALUES (:k, :v)'))\n",
    "a schema-qualified INSERT": "async def f(db):\n    await db.execute(text('INSERT INTO main.settings VALUES (:k, :v)'))\n",
    "a schema-qualified UPDATE": "async def f(db):\n    await db.execute(text('UPDATE public.settings SET value = :v'))\n",
    "MERGE INTO": "async def f(db):\n    await db.execute(text('MERGE INTO settings USING s ON true'))\n",
    "TRUNCATE": "async def f(db):\n    await db.execute(text('TRUNCATE settings'))\n",
    "a bulk insert": "def f(s, rows):\n    s.bulk_insert_mappings(Settings, rows)\n",
    "a lightweight table": "def f():\n    return sa.Table('settings', sa.MetaData())\n",
    "setattr": "async def f(db, v):\n    row = await db.get(Settings, 1)\n    setattr(row, 'value', v)\n",
    "row.value": "async def f(db, v):\n    row = await db.get(Row, 1)\n    row.value = v\n",
    "the constructor": "async def f(db, k, v):\n    await db.merge(Settings(key=k, value=v))\n",
    # Acceptance A19: a module-level name bound more than once, or by more than an import.
    "an imported constant rebound to the mode": (
        "from backend.app.services.energy_price import SETTING_KEY\nSETTING_KEY = 'spoolman_enabled'\n"
        "async def f(db, v):\n    await set_setting(db, SETTING_KEY, v)\n"
    ),
    "an imported constant rebound by a call": (
        "from backend.app.services.energy_price import SETTING_KEY\nSETTING_KEY = pick()\n"
        "async def f(db, v):\n    await set_setting(db, SETTING_KEY, v)\n"
    ),
    "an imported module rebound": (
        "from backend.app.services import energy_price as ep\nep = SimpleNamespace(SETTING_KEY='spoolman_enabled')\n"
        "async def f(db, v):\n    await set_setting(db, ep.SETTING_KEY, v)\n"
    ),
    "a name imported twice": (
        "from backend.app.services.energy_price import SETTING_KEY\n"
        "if FLAG:\n    from elsewhere import SETTING_KEY\n"
        "async def f(db, v):\n    await set_setting(db, SETTING_KEY, v)\n"
    ),
    "a constant rebound by a module-level match": (
        "match 'spoolman_enabled':\n    case str() as KEY:\n        pass\n"
        "async def f(db, v):\n    await set_setting(db, KEY, v)\n"
    ),
    "a constant shadowed by a module-level lambda": "w = lambda db, v, KEY='spoolman_enabled': set_setting(db, KEY, v)\n",
    "a constant after a star import": (
        "from backend.app.services.spoolman_source_changes import *\n"
        "async def f(db, v):\n    await set_setting(db, KEY, v)\n"
    ),
    "a constant rebound in a block": "if FLAG:\n    KEY = 'spoolman_enabled'\nasync def f(db, v):\n    await set_setting(db, KEY, v)\n",
    "a constant deleted": "del KEY\nasync def f(db, v):\n    await set_setting(db, KEY, v)\n",
    "a constant shadowed in a lambda": "def f(db, v):\n    return lambda KEY='spoolman_enabled': set_setting(db, KEY, v)\n",
    "a constant shadowed by a comprehension": (
        "async def f(db, v, keys):\n    [await set_setting(db, KEY, v) for KEY in keys]\n"
    ),
    "a constant rebound through nonlocal": (
        "async def f(db, v):\n    KEY = 'time_format'\n    def g():\n        nonlocal KEY\n        KEY = 'spoolman_enabled'\n"
        "    g()\n    await set_setting(db, KEY, v)\n"
    ),
    "a class attribute through self": (
        "class C:\n    KEY = 'spoolman_enabled'\n    async def f(self, db, v):\n        await set_setting(db, self.KEY, v)\n"
    ),
    # The model's table bound to a name the writing function uses without naming the model.
    "a table bound by an annotated assignment": (
        "T1: object = Settings.__table__\nasync def f(db):\n    await db.execute(T1.delete())\n"
    ),
    "a table bound by a walrus": (
        "if (T2 := Settings.__table__) is not None:\n    pass\nasync def f(db):\n    await db.execute(T2.update())\n"
    ),
    "a table bound by tuple unpacking": (
        "T3, *_ = Settings.__table__, None, None\nasync def f(db):\n    await db.execute(T3.insert())\n"
    ),
    # A function that names the model and writes through a table it binds any way.
    "a table bound by a loop": (
        "async def f(db):\n    for t in (Settings.__table__,):\n        await db.execute(t.delete())\n"
    ),
    "a table bound by a call": (
        "async def f(db):\n    t = (lambda: Settings.__table__)()\n    await db.execute(t.update().values(value='x'))\n"
    ),
    "a statement bound to a name": "op = sa.update\nasync def f(db):\n    await db.execute(op(Row).values(value='x'))\n",
    "a legacy query's bulk delete": "def f(db, k):\n    db.query(Settings).filter_by(key=k).delete()\n",
    "a legacy query's bulk update": "def f(db, v):\n    db.query(Row).update({'value': v})\n",
    "a helper through a dotted import": (
        "import backend.app.services.cloud_credentials\n"
        "async def f(db, c, values):\n    await backend.app.services.cloud_credentials.write(db, c, values)\n"
    ),
    "a helper through its package": (
        "from backend.app import services\n"
        "async def f(db, c, values):\n    await services.cloud_credentials.write(db, c, values)\n"
    ),
    "a helper through a relative import": (
        "from .services.cloud_credentials import write as put_values\n"
        "async def f(db, c, values):\n    await put_values(db, c, values)\n"
    ),
    "a helper's module bound to a name": (
        "cc = cloud_credentials\nasync def f(db, c, values):\n    await cc.write(db, c, values)\n"
    ),
    "a helper through a star import": (
        "from backend.app.core.schema_repair_state import *\nasync def f(db, key, v):\n    await put_record(db, key, v)\n"
    ),
    "a helper bound by an annotated assignment": "put2: object = set_setting\nasync def f(db, key, v):\n    await put2(db, key, v)\n",
    "a helper bound by a walrus": (
        "async def f(db, key, v):\n    if (put3 := set_setting):\n        await put3(db, key, v)\n"
    ),
    "one name bound to two helpers": (
        "def g():\n    global put9\n    put9 = upsert_setting\n"
        "async def f(db):\n    put9 = set_setting\n    await put9(db, 'spoolman_enabled', 'time_format')\n"
    ),
    "a helper's name rebound to another helper": (
        "set_setting = upsert_setting\nasync def f(db, v):\n    await set_setting(db, 'time_format', 'spoolman_enabled', v)\n"
    ),
    "a partial that binds the key by keyword": "def f(db):\n    return partial(set_setting, db, key='time_format')\n",
    "SQL with a comment inside": "async def f(db):\n    await db.execute(text('DELETE /* all */ FROM settings'))\n",
    "SQL with a line comment inside": "async def f(db):\n    await db.execute(text('UPDATE -- all\\n settings SET value = 1'))\n",
    "UPDATE OR REPLACE": "async def f(db):\n    await db.execute(text('UPDATE OR REPLACE settings SET value = 1'))\n",
    "TRUNCATE of two tables": "async def f(db):\n    await db.execute(text('TRUNCATE spools, ONLY public.settings'))\n",
    "DROP TABLE of two tables": "async def f(db):\n    await db.execute(text('DROP TABLE IF EXISTS spools, settings'))\n",
    "ALTER TABLE": "async def f(db):\n    await db.execute(text('ALTER TABLE settings RENAME TO settings_old'))\n",
}

# What the scan reads as a key that is not the mode: a constant stays a constant.
NOT_SEEN = {
    "a literal key": "async def f(db, v):\n    await set_setting(db, 'time_format', v)\n",
    "the module's constant": "async def f(db, v):\n    await set_setting(db, KEY, v)\n",
    "a constant imported in the function": (
        "async def f(db, v):\n"
        "    from backend.app.services.energy_price import SETTING_KEY\n"
        "    await set_setting(db, SETTING_KEY, v)\n"
    ),
    "a partial that binds the key": "def f(db):\n    return partial(set_setting, db, 'time_format')\n",
    "a constant repeated at module level": (
        "KEY = 'spoolman_enabled'\nKEY = 'time_format'\nasync def f(db, v):\n    await set_setting(db, KEY, v)\n"
    ),
    "a class attribute a method does not see": (
        "class C:\n    KEY = 'spoolman_enabled'\n    async def f(self, db, v):\n        await set_setting(db, KEY, v)\n"
    ),
    "a constant read in a nested function": (
        "def f():\n    async def g(db, v):\n        await set_setting(db, KEY, v)\n    return g\n"
    ),
    "a constant imported in the function under an alias": (
        "async def f(db, v):\n"
        "    from backend.app.services.energy_price import SETTING_KEY as KEY\n"
        "    await set_setting(db, KEY, v)\n"
    ),
    "a constant through a module the function imports": (
        "async def f(db, v):\n"
        "    import backend.app.services.energy_price as ep\n"
        "    await set_setting(db, ep.SETTING_KEY, v)\n"
    ),
    "a constant through a dotted module path": (
        "import backend.app.services.energy_price\n"
        "async def f(db, v):\n    await set_setting(db, backend.app.services.energy_price.SETTING_KEY, v)\n"
    ),
    "a constant through a relative import": (
        "from .services.energy_price import SETTING_KEY\nasync def f(db, v):\n    await set_setting(db, SETTING_KEY, v)\n"
    ),
    "a name imported twice from one place": (
        "from backend.app.services.energy_price import SETTING_KEY\n"
        "if FLAG:\n    from backend.app.services.energy_price import SETTING_KEY\n"
        "async def f(db, v):\n    await set_setting(db, SETTING_KEY, v)\n"
    ),
}

# Examples of what it cannot see (the docstring names them; the list is not complete).
BLIND = {
    "a row fetched elsewhere": "async def f(db, v):\n    row = await _row(db, 'spoolman_enabled')\n    row.value = v\n",
    "a table handed on as a parameter": (
        "async def f(db, table):\n    await db.execute(table.delete())\n"
        "async def g(db):\n    await f(db, Settings.__table__)\n"
    ),
    "a table name at run time": "async def f(db, t):\n    await db.execute(text(f'DELETE FROM {t}'))\n",
    "SQL summed from strings": "async def f(db):\n    await db.execute(text('DELETE FROM ' + 'settings'))\n",
    "a helper reached through a name at run time": (
        "async def f(db, module, v):\n    await getattr(module, 'set_setting')(db, 'spoolman_enabled', v)\n"
    ),
}


def _judged(source: str) -> set[str]:
    """The scopes of a synthetic module the scan takes for writers, the module's own included."""
    tree = ast.parse(source)
    names = _Names("synthetic.py", tree, {**_module_constants(), **_constants_of("synthetic.py", tree)})
    return {qual for qual, nodes, view in _scopes(tree, "synthetic.py") if _writes(names, nodes, view)}


def _sees(body: str) -> bool:
    return bool(_judged(_IMPORTS + body))


def test_the_scan_sees_the_forms_its_docstring_names():
    assert _judged(_IMPORTS) == set(), "the forms' own imports already look like a writer"
    assert [label for label, body in SEEN.items() if not _sees(body)] == []
    assert [label for label, body in NOT_SEEN.items() if _sees(body)] == []


def test_the_scan_is_blind_to_the_examples_its_docstring_gives():
    assert [label for label, body in BLIND.items() if _sees(body)] == []


def test_the_literal_mode_key_is_written_only_by_the_spoolman_settings_route():
    writers = set()
    for rel, names, scopes in _parsed():
        for qual, nodes, view in scopes:
            for node in nodes:
                if not isinstance(node, ast.Call):
                    continue
                helper = names.helper(node)
                keys = _helper_keys(node, helper) if helper else None
                if names.is_model(node.func, view):
                    keys = [k.value for k in node.keywords if k.arg == "key"]
                if any(names.literal(key, view) == "spoolman_enabled" for key in keys or ()):
                    writers.add((rel, qual))
    assert writers == {("api/routes/settings.py", "_write_spoolman_settings")}


def test_every_mode_writer_names_the_mode_to_its_change():
    for writer, (door, anchor_first) in sorted(MODE_WRITERS.items()):
        body = _function_source(*door)
        assert re.search(r"\.decide\([^)]*enabled=", body), f"{door}: the change of {writer} names no mode"
        for owner in anchor_first:
            assert "wait_before_anchor(" in _function_source(*owner), f"{owner} waits for no slot-ledger writer"
