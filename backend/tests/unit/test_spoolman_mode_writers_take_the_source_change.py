"""Every write of the Spoolman mode names the new mode to its change of the source (C047 A09).

The slot ledgers hold the active mode only. ``SourceChange.decide(enabled=…)`` waits for the
slot-ledger writers and empties the ledger the mode leaves; a door that writes
``spoolman_enabled`` — or deletes its row, which then reads as the default — without naming
the mode there leaves the other mode's rows behind, to be taken for the active mode's by
every reader. The reset did (A09). A door that takes the credential anchor before ``decide``
waits for the writers before it (``wait_before_anchor``, A12).

The scan finds every write of a ``settings`` row under ``backend/app`` whose key is
``spoolman_enabled`` or not a literal: ``set_setting`` / ``upsert_setting``, the ``Settings``
constructor, ``delete`` / ``update`` / ``insert(Settings)``, raw SQL, and — in a function that
names the model — an ORM ``.delete(row)`` or a ``row.value =``. Each is a MODE writer, whose
door names the mode, or is listed in NOT_THE_MODE with the keys it writes. A new writer fails
here until it is classified.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
MODEL_NAMES = {"Settings", "SettingsModel"}
SQL_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?settings\"?\b", re.IGNORECASE)

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
    ("api/routes/orca_cloud.py", "_upsert_settings"): "the Orca Cloud account keys of its callers",
    ("api/routes/settings.py", "set_setting"): "the helper: its callers are scanned",
    ("api/routes/support.py", "_set_debug_setting"): "debug_logging_enabled, debug_logging_enabled_at",
    ("cli.py", "_reset_admin"): "setup_completed, auth_enabled",
    ("cli.py", "_reset_password"): "user_<id>_email_2fa_enabled",
    ("core/credential_config.py", "initialize_credential_config_anchor"): "the credential anchor row",
    ("core/credential_config.py", "lock_credential_config"): "the credential anchor row",
    ("core/db_portable.py", "_export_pg_to_sqlite"): "the portable-backup marker, in the exported copy",
    ("core/schema_repair_state.py", "claim_attempt"): "the private import-repair record",
    ("core/schema_repair_state.py", "fail_attempt"): "the private import-repair record",
    ("core/schema_repair_state.py", "finish_attempt"): "the private import-repair record",
    ("core/schema_repair_state.py", "put_record"): "the private import-repair record",
    ("main.py", "_application_lifespan._reconcile_locale_on_startup"): "notification_language",
    ("main.py", "_save_ams_drying_latch"): "the AMS drying latch",
    ("services/archive_cleanup_service.py", "ArchiveCleanupService._stamp_last_run"): "the cleanup's last-run stamps",
    ("services/archive_purge.py", "ArchivePurgeService.set_retention_days"): "the archive trash retention",
    ("services/cloud_credentials.py", "write"): "the cloud account keys and their generation",
    ("services/connector_submissions.py", "server_identity"): "the connector server id and storage epoch",
    ("services/connector_submissions.py", "stage_storage_epoch"): "the connector storage epoch",
    ("services/email_service.py", "save_smtp_settings"): "the SMTP keys",
    ("services/energy_price.py", "set_price"): "energy_cost_per_kwh",
    ("services/library_trash.py", "LibraryTrashService._write_setting"): "the library trash keys",
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


def _literal(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _may_be_the_mode(key_node) -> bool:
    key = _literal(key_node)
    return key is None or key == "spoolman_enabled"


def _called(node) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _writes(nodes) -> bool:
    names = {n.id for n in nodes if isinstance(n, ast.Name)}
    names_model = bool(names & MODEL_NAMES)
    for node in nodes:
        if isinstance(node, ast.Call):
            name = _called(node)
            if name == "set_setting" and len(node.args) >= 2 and _may_be_the_mode(node.args[1]):
                return True
            if name == "upsert_setting" and len(node.args) >= 3 and _may_be_the_mode(node.args[2]):
                return True
            if name in MODEL_NAMES:
                key = next((k.value for k in node.keywords if k.arg == "key"), None)
                if key is not None and _may_be_the_mode(key):
                    return True
            if (
                name in {"delete", "update", "insert"}
                and isinstance(node.func, ast.Name)
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in MODEL_NAMES
            ):
                return True
            if names_model and name == "delete" and isinstance(node.func, ast.Attribute) and node.args:
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL_WRITE.search(node.value):
            return True
        if names_model and isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Attribute) and t.attr == "value" for t in node.targets):
                return True
    return False


def _found():
    found = set()
    for rel, source in _modules():
        for qual, nodes in _scopes(ast.parse(source)):
            if _writes(nodes):
                found.add((rel, qual))
    return found


def _function_source(rel: str, qual: str) -> str:
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
    return ast.get_source_segment(source, node) or ""


def test_every_writer_of_a_settings_row_that_may_be_the_mode_is_classified():
    found = _found()
    known = set(MODE_WRITERS) | set(NOT_THE_MODE)
    assert found - known == set(), "a new writer of a settings row: a MODE writer, or the keys it writes"
    assert known - found == set(), "a listed writer is gone: drop it from the list"
    assert not set(MODE_WRITERS) & set(NOT_THE_MODE)


def test_the_literal_mode_key_is_written_only_by_the_spoolman_settings_route():
    writers = set()
    for rel, source in _modules():
        for qual, nodes in _scopes(ast.parse(source)):
            for node in nodes:
                if isinstance(node, ast.Call) and _called(node) in {"set_setting", "upsert_setting"}:
                    index = 1 if _called(node) == "set_setting" else 2
                    if len(node.args) > index and _literal(node.args[index]) == "spoolman_enabled":
                        writers.add((rel, qual))
    assert writers == {("api/routes/settings.py", "_write_spoolman_settings")}


def test_every_mode_writer_names_the_mode_to_its_change():
    for writer, (door, anchor_first) in sorted(MODE_WRITERS.items()):
        body = _function_source(*door)
        assert re.search(r"\.decide\([^)]*enabled=", body), f"{door}: the change of {writer} names no mode"
        for owner in anchor_first:
            assert "wait_before_anchor(" in _function_source(*owner), f"{owner} waits for no slot-ledger writer"
