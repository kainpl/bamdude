"""Nothing but ``services/library_file_photos.py`` writes a library file's photos.

Three scans of the tree:

* only the writer (and the model, which declares the table) creates, changes or
  deletes a ``LibraryFilePhoto`` — the route module may hold the type, to read;
* only the writer names the table in a string or the folder under the library's
  root;
* every place that purges a library file — recognised by the call every one of
  them already makes, ``purge_file_product_links`` — tells the writer. SQLite
  runs no FK actions, and a photo folder is named by an id the next file may be
  given, so a purge that forgets the photos would hand them to a stranger.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
WRITER = APP / "services" / "library_file_photos.py"
MODEL = APP / "models" / "library_file_photo.py"
ROUTES = APP / "api" / "routes" / "library_photos.py"
SCHEMA = APP / "schemas" / "library_file_photo.py"

# Who may import the class at all. The routes read through it; they are scanned below.
MAY_HOLD_THE_CLASS = {WRITER, MODEL, ROUTES}
# Who may spell the table's name in a string.
MAY_NAME_THE_TABLE = {WRITER, MODEL}
COLUMNS = {
    "library_file_id",
    "stored_name",
    "original_name",
    "content_type",
    "file_size",
    "width",
    "height",
    "caption",
    "uploaded_by_id",
    "uploaded_by_name",
}


def _sources():
    for path in APP.rglob("*.py"):
        if "migrations" not in path.parts:
            yield path, path.read_text(encoding="utf-8")


def _names(tree: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def test_only_the_writer_and_its_readers_hold_the_class():
    offenders = [
        str(path.relative_to(APP))
        for path, source in _sources()
        if path not in MAY_HOLD_THE_CLASS
        and "LibraryFilePhoto" in _names(ast.parse(source)) - {"LibraryFilePhotoResponse"}
    ]
    assert offenders == []


def _writes(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(s, ast.Attribute) and s.attr in COLUMNS for t in targets for s in ast.walk(t)):
                hits.append(node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            names_the_class = any(isinstance(a, ast.Name) and a.id == "LibraryFilePhoto" for a in node.args)
            if called == "LibraryFilePhoto":
                hits.append(node.lineno)  # a row constructed
            elif called in {"delete", "update", "insert"} and names_the_class:
                hits.append(node.lineno)  # a Core write
            elif (
                called in {"add", "add_all", "delete", "merge", "commit", "flush"}
                and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in {"db", "session"}
            ):
                # ``db.delete(photo)`` / ``db.add(photo)`` / ``db.commit()`` — the session's writes are the writer's.
                hits.append(node.lineno)
    return hits


def test_the_route_module_reads_and_never_writes():
    assert _writes(ast.parse(ROUTES.read_text(encoding="utf-8"))) == []


def test_only_the_writer_names_the_table_or_the_folder():
    offenders = []
    for path, source in _sources():
        if path in MAY_NAME_THE_TABLE:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # A docstring may mention it; an executable string may not.
                if "library_file_photos" in node.value and "\n" not in node.value and " " in node.value.strip():
                    offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr in {"photos_root", "_remove_folders", "_write_files"}:
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert offenders == []


def _calls(function: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            func = node.func
            found.add(func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else "")
    return found


def test_every_purge_of_a_library_file_tells_the_writer():
    purging, forgetful = [], []
    for path, source in _sources():
        if path.name == "product_sync.py":
            continue  # where purge_file_product_links is defined
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = _calls(node)
            if "purge_file_product_links" not in calls:
                continue
            where = f"{path.relative_to(APP).as_posix()}::{node.name}"
            purging.append(where)
            if "forget_files" not in calls:
                forgetful.append(where)
    # The scan must be looking at something: these are the purges known today.
    assert {
        "services/library_trash.py::_sweep",
        "services/library_trash.py::trash_or_purge",
        "services/library_trash.py::hard_delete_now",
        "services/library_scan.py::remove_vanished",
        "api/routes/users.py::delete_user",
    } <= set(purging), sorted(purging)
    assert forgetful == []


def test_a_deleted_user_is_detached_in_code():
    """``uploaded_by_id`` is ON DELETE SET NULL, which SQLite never applies."""
    tree = ast.parse((APP / "api" / "routes" / "users.py").read_text(encoding="utf-8"))
    delete_user = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "delete_user"
    )
    owners = {
        ast.unparse(node.func.value)
        for node in ast.walk(delete_user)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "detach_user"
    }
    assert "library_file_photos" in owners
