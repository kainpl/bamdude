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
  scan can read — a literal, or a constant of its own module or one imported — positional
  or keyword, under any import or assignment alias, through ``functools.partial`` too; a
  helper handed on as a value (acceptance A15, A17). A name the function, or one around it,
  binds itself (a parameter, an assignment, a loop, ``with``, ``except``, a comprehension) is
  not read as the module's constant, and a module name rebound anywhere (``global``) is none;
- the ``Settings`` constructor (any alias) with such a key, or with no readable key;
- a SQLAlchemy ``delete`` / ``update`` / ``insert`` of the model or its table — ``Settings``,
  ``Settings.__table__``, a name bound to either, ``metadata.tables["settings"]`` — under any
  alias, a dialect's insert through its module too; ``.delete()`` / ``.update()`` /
  ``.insert()`` called on the table; a bulk ``*_mappings`` / ``bulk_save_objects`` of the
  model; a lightweight ``Table("settings", …)``;
- SQL text that writes the table, with or without a schema: ``INSERT [OR …] INTO``,
  ``REPLACE INTO``, ``MERGE INTO``, ``UPDATE``, ``DELETE FROM``, ``TRUNCATE``, ``DROP TABLE``,
  ``COPY``;
- in a function that names the model: an ORM ``.delete(row)``, a ``row.value =``, a
  ``setattr(row, "value", …)``.

Each is a MODE writer, whose door names the mode, a keyed helper (its callers are scanned), or
is listed in NOT_THE_MODE with the keys it writes; a new writer fails here until it is
classified. SEEN pins the forms above. The scan cannot prove that no other form exists; BLIND
pins examples of what it cannot see: a value carried through variables to an object fetched
in a function that does not name the model, SQL or a table name assembled at run time, a
helper reached through a name built at run time (``getattr``). The whole-database restore is
outside the scan that way: it replaces the database below it — on SQLite the file, through
the backup API; on PostgreSQL every table, by names read at run time — and asks for a restart.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
MODEL_MODULE = "backend.app.models.settings"
SQL_WRITE = re.compile(
    r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|REPLACE\s+INTO|MERGE\s+INTO|UPDATE(\s+ONLY)?|DELETE\s+FROM(\s+ONLY)?"
    r"|TRUNCATE(\s+TABLE)?(\s+ONLY)?|DROP\s+TABLE(\s+IF\s+EXISTS)?|COPY)"
    r"\s+([\"`\[]?\w+[\"`\]]?\.)?[\"`\[]?settings[\"`\]]?\b",
    re.IGNORECASE,
)
STATEMENTS = {"delete", "update", "insert"}
BULK = {"bulk_insert_mappings", "bulk_update_mappings", "bulk_save_objects"}

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


def _constants_of(rel: str, tree) -> dict[tuple[str, str], str]:
    """(file, name) → the string a module-level ``NAME = "…"`` (or a sum of such) binds.

    A name the module binds any other way as well — a loop, an import, a definition, an
    assignment inside a block — or that a function rebinds through ``global`` is no constant.
    """
    local: dict[str, str] = {}
    folded: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        value = _fold(node.value, local)
        for target in targets:
            if isinstance(target, ast.Name):
                folded.add(id(target))
                if value is None:
                    local.pop(target.id, None)
                else:
                    local[target.id] = value
    rebound = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    for node in _module_level(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in folded:
            rebound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            rebound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or (
            isinstance(node, ast.ExceptHandler) and node.name
        ):
            rebound.add(node.name)
    return {(rel, name): value for name, value in local.items() if name not in rebound}


@functools.cache
def _trees():
    return tuple((rel, ast.parse(source)) for rel, source in _modules())


@functools.cache
def _module_constants() -> dict[tuple[str, str], str]:
    constants = {}
    for rel, tree in _trees():
        constants.update(_constants_of(rel, tree))
    return constants


def _own_view(nodes) -> dict[str, tuple]:
    """How one function binds names itself: ``("import", (file, name), module)`` or ``("local",)``.

    A name both imported and bound another way in the same function is a local.
    """
    imports: dict[str, tuple] = {}
    local: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                file = _rel(node.module)
                imports[name] = ("import", (file, alias.name), f"{node.module}.{alias.name}") if file else ("local",)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = ("import", None, alias.name)
                else:
                    local.add(alias.name.split(".")[0])
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
    """What one module's names are bound to by its imports."""

    def __init__(self, rel: str, tree: ast.AST, constants: dict):
        self.rel, self.constants = rel, constants
        imports, assignments, own = [], [], False
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                imports.append(node)
            elif isinstance(node, ast.Assign):
                assignments.append(node)
            elif isinstance(node, ast.ClassDef) and node.name == "Settings":
                own = True
        # The model's name unless the module has a class of its own by that name (core/config.py).
        self.models: set[str] = set() if own else {"Settings"}
        self.statements = {name: name for name in STATEMENTS}  # local name → delete / update / insert
        self.sqlalchemy_names: set[str] = set()  # local names of sqlalchemy modules and their members
        self.helpers: dict[str, str] = {}  # local name → keyed helper
        # Imports anywhere in the module say what a name may be (helpers, the model); only
        # the module-level ones resolve a constant -- a function's own are in its view.
        self.modules_anywhere: dict[str, str] = {}
        self.modules: dict[str, str] = {}  # local name → the dotted module it binds
        self.imported: dict[str, tuple[str, str]] = {}  # local name → (file, name)
        module_level = {id(node) for node in _module_level(tree) if isinstance(node, (ast.ImportFrom, ast.Import))}
        for node in imports:
            top = id(node) in module_level
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if node.module in {MODEL_MODULE, "backend.app.models"} and alias.name == "Settings":
                        self.models.add(local)
                    if (node.module or "").startswith("sqlalchemy"):
                        if alias.name in STATEMENTS:
                            self.statements[local] = alias.name
                        else:
                            self.sqlalchemy_names.add(local)
                    helper = KEYED_HELPERS.get(alias.name)
                    if helper and helper[4] in {None, node.module}:
                        self.helpers[local] = alias.name
                    if _rel(node.module) is not None:
                        self.modules_anywhere[local] = f"{node.module}.{alias.name}"
                        if top:
                            self.imported[local] = (_rel(node.module), alias.name)
                            self.modules[local] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "sqlalchemy":
                        self.sqlalchemy_names.add(alias.asname or "sqlalchemy")
                    if alias.asname:
                        self.modules_anywhere[alias.asname] = alias.name
                        if top:
                            self.modules[alias.asname] = alias.name
        changed = True
        while changed:
            # ``put = set_setting``, ``t = Settings.__table__``: a name bound to a helper or to the
            # model or its table is that helper, or the model.
            changed = False
            for node in assignments:
                targets = {target.id for target in node.targets if isinstance(target, ast.Name)}
                if self.is_model(node.value) and not targets <= self.models:
                    self.models |= targets
                    changed = True
                helper = self.alias_of_helper(node.value)
                if helper is not None and not targets <= self.helpers.keys():
                    self.helpers.update(dict.fromkeys(targets, helper))
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
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            binding = view.get(node.value.id)
            module = (binding[2] if binding[0] == "import" else None) if binding else self.modules.get(node.value.id)
            if module and _rel(module) is not None:
                return self.constants.get((_rel(module), node.attr))
        return None

    def may_be_the_mode(self, node, view=None) -> bool:
        key = self.literal(node, view)
        return key is None or key == "spoolman_enabled"

    def is_model(self, node, view=None) -> bool:
        """The model, its table, or a name bound to either."""
        if isinstance(node, ast.Name):
            return node.id in self.models
        if isinstance(node, ast.Attribute) and node.attr == "__table__":
            return self.is_model(node.value, view)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "tables":
            return self.literal(node.slice, view) == "settings"  # ``metadata.tables["settings"]``
        return isinstance(node, ast.Attribute) and node.attr == "Settings"

    def statement(self, call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            return self.statements.get(func.id)
        if isinstance(func, ast.Attribute) and func.attr in STATEMENTS and _root(func.value) in self.sqlalchemy_names:
            return func.attr  # ``sa.delete``, ``postgresql.insert``, ``sa.dialects.sqlite.insert``
        return None

    def helper_ref(self, func) -> str | None:
        """The keyed helper a name or an attribute refers to, or None."""
        if isinstance(func, ast.Name):
            if func.id in self.helpers:
                return self.helpers[func.id]
            spec = KEYED_HELPERS.get(func.id)
            if spec and spec[0][0] == self.rel and spec[0][1] == func.id:
                return func.id  # in its own module
            return None
        if isinstance(func, ast.Attribute):
            spec = KEYED_HELPERS.get(func.attr)
            if spec is None:
                return None
            if spec[4] is None:
                return func.attr
            if isinstance(func.value, ast.Name) and self.modules_anywhere.get(func.value.id) == spec[4]:
                return func.attr
        return None

    def alias_of_helper(self, node) -> str | None:
        helper = self.helper_ref(node)
        if helper is None and isinstance(node, ast.Name):
            spec = KEYED_HELPERS.get(node.id)
            if spec and spec[4] is None:
                helper = node.id
        return helper

    def helper(self, call: ast.Call) -> str | None:
        return self.helper_ref(call.func)


def _scopes(tree):
    """(qualified name, nodes, view) of the module and every function or class.

    A nested function's nodes are its own; its view is how its names are bound — by itself
    and by the functions around it (a class body is not around its methods). The module's
    view is empty: its names are read through its constants and imports.
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
            own = _own_view(nodes)
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
    _definition, index, keyword, kind, _module = KEYED_HELPERS[helper]
    argument = _argument(call, index, keyword)
    if argument is None:
        return None
    if kind == "key":
        return [argument]
    if isinstance(argument, ast.Dict) and None not in argument.keys:
        return list(argument.keys)
    return None


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
        elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
            read.add(id(node.value))
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
                # ``partial(set_setting, db)``: the key is read from what the partial binds.
                helper = names.helper_ref(node.args[0])
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
            if names_model and _called(node) == "delete" and isinstance(node.func, ast.Attribute) and node.args:
                return True
            if names_model and _called(node) == "setattr" and len(node.args) >= 2:
                if names.literal(node.args[1], view) in {"value", None}:
                    return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL_WRITE.search(node.value):
            return True
        if names_model and isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr == "value" for t in targets):
                return True
    return False


@functools.cache
def _parsed():
    constants = _module_constants()
    return tuple((rel, _Names(rel, tree, constants), _scopes(tree)) for rel, tree in _trees())


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
}

# Examples of what it cannot see (the docstring names them; the list is not complete).
BLIND = {
    "a row fetched elsewhere": "async def f(db, v):\n    row = await _row(db, 'spoolman_enabled')\n    row.value = v\n",
    "a table name at run time": "async def f(db, t):\n    await db.execute(text(f'DELETE FROM {t}'))\n",
    "a helper reached through a name at run time": (
        "async def f(db, module, v):\n    await getattr(module, 'set_setting')(db, 'spoolman_enabled', v)\n"
    ),
}


def _sees(body: str) -> bool:
    tree = ast.parse(_IMPORTS + body)
    names = _Names("synthetic.py", tree, {**_module_constants(), **_constants_of("synthetic.py", tree)})
    return any(_writes(names, nodes, view) for qual, nodes, view in _scopes(tree) if qual.split(".")[0] == "f")


def test_the_scan_sees_the_forms_its_docstring_names():
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
