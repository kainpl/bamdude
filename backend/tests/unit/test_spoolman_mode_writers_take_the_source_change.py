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
  scan can read — a literal, or a module constant of its own module or imported — positional
  or keyword, under any import alias (acceptance A15);
- the ``Settings`` constructor (any alias) with such a key, or with no readable key;
- a SQLAlchemy ``delete`` / ``update`` / ``insert`` of ``Settings`` or ``Settings.__table__``
  (any alias, dialect inserts too), ``Settings.__table__.delete()`` and its siblings, a bulk
  ``*_mappings`` / ``bulk_save_objects`` of the model, a lightweight ``Table("settings", …)``;
- SQL text that writes the table: ``INSERT [OR …] INTO``, ``REPLACE INTO``, ``UPDATE``,
  ``DELETE FROM``;
- in a function that names the model: an ORM ``.delete(row)``, a ``row.value =``, a
  ``setattr(row, "value", …)``.

Each is a MODE writer, whose door names the mode, a keyed helper (its callers are scanned), or
is listed in NOT_THE_MODE with the keys it writes; a new writer fails here until it is
classified. What the scan cannot see: a value carried through variables to an object fetched
in a function that does not name the model, and a table name assembled at run time — the
whole-database restore, which replaces every ``settings`` row with the backup's and restarts.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
MODEL_MODULE = "backend.app.models.settings"
SQL_WRITE = re.compile(
    r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"`]?settings[\"`]?\b", re.IGNORECASE
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


def _module_constants() -> dict[tuple[str, str], str]:
    """(file, name) → the string a module-level ``NAME = "…"`` (or a sum of such) binds."""
    constants = {}
    for rel, source in _modules():
        local: dict[str, str] = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            else:
                continue
            value = _fold(node.value, local)
            for target in targets:
                if isinstance(target, ast.Name):
                    if value is None:
                        local.pop(target.id, None)
                    else:
                        local[target.id] = value
        constants.update({(rel, name): value for name, value in local.items()})
    return constants


class _Names:
    """What one module's names are bound to by its imports."""

    def __init__(self, rel: str, tree: ast.AST, constants: dict):
        self.rel, self.constants = rel, constants
        # The model's name unless the module has a class of its own by that name (core/config.py).
        own = any(isinstance(node, ast.ClassDef) and node.name == "Settings" for node in ast.walk(tree))
        self.models: set[str] = set() if own else {"Settings"}
        self.statements = {name: name for name in STATEMENTS}  # local name → delete / update / insert
        self.sqlalchemy_modules: set[str] = set()
        self.helpers: dict[str, str] = {}  # local name → keyed helper
        self.modules: dict[str, str] = {}  # local name → the dotted module it binds
        self.imported: dict[str, tuple[str, str]] = {}  # local name → (file, name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if node.module in {MODEL_MODULE, "backend.app.models"} and alias.name == "Settings":
                        self.models.add(local)
                    if (node.module or "").startswith("sqlalchemy") and alias.name in STATEMENTS:
                        self.statements[local] = alias.name
                    helper = KEYED_HELPERS.get(alias.name)
                    if helper and helper[4] in {None, node.module}:
                        self.helpers[local] = alias.name
                    if _rel(node.module) is not None:
                        self.imported[local] = (_rel(node.module), alias.name)
                        self.modules[local] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlalchemy":
                        self.sqlalchemy_modules.add(alias.asname or alias.name)
                    if alias.asname:
                        self.modules[alias.asname] = alias.name
        for node in ast.walk(tree):
            # ``put = set_setting``: a name bound to a helper or the model is that helper or the model.
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                bound = node.value.id if isinstance(node.value, ast.Name) else node.value.attr
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if bound in self.helpers or (bound in KEYED_HELPERS and KEYED_HELPERS[bound][4] is None):
                        self.helpers[target.id] = self.helpers.get(bound, bound)
                    if bound in self.models:
                        self.models.add(target.id)

    def literal(self, node) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            if (self.rel, node.id) in self.constants:
                return self.constants[(self.rel, node.id)]
            if node.id in self.imported:
                return self.constants.get(self.imported[node.id])
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = self.modules.get(node.value.id)
            if module and _rel(module) is not None:
                return self.constants.get((_rel(module), node.attr))
        return None

    def may_be_the_mode(self, node) -> bool:
        key = self.literal(node)
        return key is None or key == "spoolman_enabled"

    def is_model(self, node) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.models
        if isinstance(node, ast.Attribute) and node.attr == "__table__":
            return self.is_model(node.value)
        return isinstance(node, ast.Attribute) and node.attr == "Settings"

    def statement(self, call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            return self.statements.get(func.id)
        if (
            isinstance(func, ast.Attribute)
            and func.attr in STATEMENTS
            and isinstance(func.value, ast.Name)
            and func.value.id in self.sqlalchemy_modules
        ):
            return func.attr
        return None

    def helper(self, call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in self.helpers:
                return self.helpers[func.id]
            spec = KEYED_HELPERS.get(func.id)
            if spec and spec[0][0] == self.rel and spec[0][1] == func.id:
                return func.id  # called in its own module
            return None
        if isinstance(func, ast.Attribute):
            spec = KEYED_HELPERS.get(func.attr)
            if spec is None:
                return None
            if spec[4] is None:
                return func.attr
            if isinstance(func.value, ast.Name) and self.modules.get(func.value.id) == spec[4]:
                return func.attr
        return None


def _scopes(tree):
    """(qualified name, nodes) of the module and every function, a nested function's nodes its own."""
    out = []

    def collect(owner, qual):
        nodes, stack = [], list(ast.iter_child_nodes(owner))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                collect(node, f"{qual}.{node.name}" if qual else node.name)
                continue
            nodes.append(node)
            stack.extend(ast.iter_child_nodes(node))
        out.append((qual or "<module>", nodes))

    collect(tree, "")
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


def _writes(names: _Names, nodes) -> bool:
    names_model = any(isinstance(n, (ast.Name, ast.Attribute)) and names.is_model(n) for n in nodes)
    for node in nodes:
        if isinstance(node, ast.Call):
            helper = names.helper(node)
            if helper is not None:
                keys = _helper_keys(node, helper)
                if keys is None or any(names.may_be_the_mode(key) for key in keys):
                    return True
            if names.is_model(node.func):
                key = next((k.value for k in node.keywords if k.arg == "key"), None)
                if key is None or names.may_be_the_mode(key):
                    return True
            if names.statement(node) and node.args and names.is_model(node.args[0]):
                return True
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in STATEMENTS
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "__table__"
                and names.is_model(node.func.value)
            ):
                return True
            if _called(node) in BULK and node.args and names.is_model(node.args[0]):
                return True
            if _called(node) in {"Table", "table"} and node.args and names.literal(node.args[0]) == "settings":
                return True
            if names_model and _called(node) == "delete" and isinstance(node.func, ast.Attribute) and node.args:
                return True
            if names_model and _called(node) == "setattr" and len(node.args) >= 2:
                if names.literal(node.args[1]) in {"value", None}:
                    return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL_WRITE.search(node.value):
            return True
        if names_model and isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr == "value" for t in targets):
                return True
    return False


def _parsed():
    constants = _module_constants()
    for rel, source in _modules():
        tree = ast.parse(source)
        yield rel, _Names(rel, tree, constants), _scopes(tree)


def _found():
    return {(rel, qual) for rel, names, scopes in _parsed() for qual, nodes in scopes if _writes(names, nodes)}


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
import sqlalchemy as sa
from sqlalchemy import delete as sa_delete, insert, text, update
from backend.app.api.routes.settings import set_setting
from backend.app.core.db_dialect import upsert_setting
from backend.app.models.settings import Settings
from backend.app.models.settings import Settings as Row
from backend.app.services import cloud_credentials
from backend.app.services.cloud_credentials import write
put = set_setting
"""

# The forms the docstring says the scan sees (acceptance A15: the reviewer's probe of the old scan).
SEEN = {
    "a variable key": "async def f(db, key, v):\n    await set_setting(db, key, v)\n",
    "the mode by keyword": "async def f(db, v):\n    await set_setting(db, key='spoolman_enabled', value=v)\n",
    "a helper bound to a name": "async def f(db, key, v):\n    await put(db, key, v)\n",
    "upsert_setting": "async def f(db, key, v):\n    await upsert_setting(db, Settings, key, v)\n",
    "a dict of keys": "async def f(db, c, v):\n    await write(db, c, {'spoolman_enabled': v})\n",
    "a dict built elsewhere": "async def f(db, c, values):\n    await cloud_credentials.write(db, c, values)\n",
    "update(Settings)": "async def f(db):\n    await db.execute(update(Settings).values(value='x'))\n",
    "Settings.__table__.delete()": "async def f(db):\n    await db.execute(Settings.__table__.delete())\n",
    "Settings.__table__.update()": "async def f(db):\n    await db.execute(Settings.__table__.update().values(value='x'))\n",
    "insert(Settings.__table__)": "async def f(db):\n    await db.execute(insert(Settings.__table__).values(key='k'))\n",
    "an aliased delete": "async def f(db):\n    await db.execute(sa_delete(Settings))\n",
    "sa.delete": "async def f(db):\n    await db.execute(sa.delete(Row))\n",
    "INSERT INTO": "async def f(db):\n    await db.execute(text('INSERT INTO settings (key, value) VALUES (:k, :v)'))\n",
    "INSERT OR REPLACE": "async def f(db):\n    await db.execute(text('INSERT OR REPLACE INTO settings VALUES (:k, :v)'))\n",
    "REPLACE INTO": "async def f(db):\n    await db.execute(text('REPLACE INTO \"settings\" VALUES (:k, :v)'))\n",
    "a bulk insert": "def f(s, rows):\n    s.bulk_insert_mappings(Settings, rows)\n",
    "a lightweight table": "def f():\n    return sa.Table('settings', sa.MetaData())\n",
    "setattr": "async def f(db, v):\n    row = await db.get(Settings, 1)\n    setattr(row, 'value', v)\n",
    "row.value": "async def f(db, v):\n    row = await db.get(Row, 1)\n    row.value = v\n",
    "the constructor": "async def f(db, k, v):\n    await db.merge(Settings(key=k, value=v))\n",
}

# What the docstring says it cannot see.
BLIND = {
    "a row fetched elsewhere": "async def f(db, v):\n    row = await _row(db, 'spoolman_enabled')\n    row.value = v\n",
    "a table name at run time": "async def f(db, t):\n    await db.execute(text(f'DELETE FROM {t}'))\n",
}


def _sees(body: str) -> bool:
    tree = ast.parse(_IMPORTS + body)
    names = _Names("synthetic.py", tree, {})
    return any(_writes(names, nodes) for qual, nodes in _scopes(tree) if qual == "f")


def test_the_scan_sees_the_forms_its_docstring_names():
    assert [label for label, body in SEEN.items() if not _sees(body)] == []
    assert not _sees("async def f(db, v):\n    await set_setting(db, 'time_format', v)\n")


def test_the_scan_is_blind_only_where_its_docstring_says():
    assert [label for label, body in BLIND.items() if _sees(body)] == []


def test_the_literal_mode_key_is_written_only_by_the_spoolman_settings_route():
    writers = set()
    for rel, names, scopes in _parsed():
        for qual, nodes in scopes:
            for node in nodes:
                if not isinstance(node, ast.Call):
                    continue
                helper = names.helper(node)
                keys = _helper_keys(node, helper) if helper else None
                if names.is_model(node.func):
                    keys = [k.value for k in node.keywords if k.arg == "key"]
                if any(names.literal(key) == "spoolman_enabled" for key in keys or ()):
                    writers.add((rel, qual))
    assert writers == {("api/routes/settings.py", "_write_spoolman_settings")}


def test_every_mode_writer_names_the_mode_to_its_change():
    for writer, (door, anchor_first) in sorted(MODE_WRITERS.items()):
        body = _function_source(*door)
        assert re.search(r"\.decide\([^)]*enabled=", body), f"{door}: the change of {writer} names no mode"
        for owner in anchor_first:
            assert "wait_before_anchor(" in _function_source(*owner), f"{owner} waits for no slot-ledger writer"
