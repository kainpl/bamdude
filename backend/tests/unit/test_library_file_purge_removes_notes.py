"""Every purge of a library file removes its notes, whichever way it deletes the row.

``library_file_notes.library_file_id`` is ``ON DELETE CASCADE``; SQLite runs no
FK actions. ``db.delete(file)`` on a loaded row removes the notes through the
ORM cascade of ``LibraryFile.file_notes``; a Core ``delete(LibraryFile)`` goes
past it, so a function that issues one must call ``forget_file_notes`` — or the
notes wait for the next file given that id.

A purge is recognised, as in ``test_library_file_photos_have_one_writer.py``, by
the call every one of them makes: ``purge_file_product_links``.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


def _sources():
    for path in APP.rglob("*.py"):
        if "migrations" not in path.parts:
            yield path, path.read_text(encoding="utf-8")


def _functions(tree: ast.AST):
    return (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))


def _calls(function: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            func = node.func
            found.add(func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else "")
    return found


def _bulk_deletes_files(function: ast.AST) -> bool:
    """A Core ``delete(LibraryFile)`` — the delete the ORM cascade never sees."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "delete"
        and any(isinstance(arg, ast.Name) and arg.id == "LibraryFile" for arg in node.args)
        for node in ast.walk(function)
    )


def _deletes_through_the_session(function: ast.AST) -> bool:
    """``db.delete(row)`` — the ORM delete, which cascades to the notes."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"db", "session"}
        for node in ast.walk(function)
    )


def test_a_bulk_delete_of_library_files_removes_their_notes():
    bulk, forgetful = [], []
    for path, source in _sources():
        for function in _functions(ast.parse(source)):
            if not _bulk_deletes_files(function):
                continue
            where = f"{path.relative_to(APP).as_posix()}::{function.name}"
            bulk.append(where)
            if "forget_file_notes" not in _calls(function):
                forgetful.append(where)
    # The scan must be looking at something: these are the bulk deletes known today.
    assert bulk == []  # All library DELETEs use the shared flush-only phase.
    assert forgetful == []


def test_every_purge_removes_the_notes_one_way_or_the_other():
    purging, forgetful = [], []
    for path, source in _sources():
        if path.name == "product_sync.py":
            continue  # where purge_file_product_links is defined
        for function in _functions(ast.parse(source)):
            calls = _calls(function)
            if "purge_file_product_links" not in calls:
                continue
            where = f"{path.relative_to(APP).as_posix()}::{function.name}"
            purging.append(where)
            if "forget_file_notes" not in calls and not _deletes_through_the_session(function):
                forgetful.append(where)
    assert set(purging) == {"services/content_delete_rows.py::library_rows"}
    assert forgetful == []


def test_the_orm_delete_still_cascades_to_the_notes():
    """The sites that delete through the session lean on this relationship."""
    from backend.app.models.library import LibraryFile

    relationship = LibraryFile.__mapper__.relationships["file_notes"]
    assert relationship.cascade.delete
    assert relationship.passive_deletes is False
