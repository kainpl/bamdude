"""Every purge of a library file removes its import record (m235).

``library_file_import_meta.library_file_id`` is ``ON DELETE CASCADE``; SQLite runs no FK actions, and
the next file may be given the purged one's id — it would then show another file's creator and
licence. A purge is recognised, as in ``test_library_file_purge_removes_notes.py``, by the call every
one of them makes: ``purge_file_product_links``.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"


def _calls(function: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            found.add(f"{owner.id}.{node.func.attr}" if isinstance(owner, ast.Name) else node.func.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            found.add(node.func.id)
    return found


def test_every_purge_forgets_the_import_record():
    purging, forgetful = [], []
    for path in APP.rglob("*.py"):
        if "migrations" in path.parts or path.name == "product_sync.py":
            continue
        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = _calls(function)
            if "purge_file_product_links" not in calls:
                continue
            where = f"{path.relative_to(APP).as_posix()}::{function.name}"
            purging.append(where)
            if "library_import_meta.forget_files" not in calls:
                forgetful.append(where)
    assert {
        "services/library_trash.py::_sweep",
        "services/library_trash.py::trash_or_purge",
        "services/library_trash.py::hard_delete_now",
        "services/library_scan.py::remove_vanished",
        "api/routes/users.py::delete_user",
    } <= set(purging), sorted(purging)
    assert forgetful == []


def test_only_the_writer_touches_the_table():
    offenders = []
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP).as_posix()
        if relative in {"services/library_import_meta.py", "models/library_file_import_meta.py"}:
            continue
        if "migrations" in path.parts or relative == "core/database.py":
            continue
        if "LibraryFileImportMeta" in path.read_text(encoding="utf-8"):
            offenders.append(relative)
    assert offenders == []


@pytest.mark.asyncio
async def test_forgetting_removes_the_rows_of_those_files_only(db_session):
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile
    from backend.app.models.library_file_import_meta import LibraryFileImportMeta
    from backend.app.services import library_import_meta

    files = [
        LibraryFile(filename=f"{n}.stl", file_path=f"library/files/{n}.stl", file_type="stl", file_size=1)
        for n in range(2)
    ]
    db_session.add_all(files)
    await db_session.flush()
    for file in files:
        await library_import_meta.record(
            db_session,
            library_file_id=file.id,
            source_type="manyfold",
            origin=library_import_meta.ImportOrigin(
                source_model_id="x7q3k2pdbm4z", title="  Cable\n Clip ", creator="Jane Doe", license="CC-BY-4.0"
            ),
        )
    # a second record for the same file changes nothing
    await library_import_meta.record(
        db_session,
        library_file_id=files[0].id,
        source_type="manyfold",
        origin=library_import_meta.ImportOrigin(source_model_id="other", title="Other"),
    )
    kept = await library_import_meta.for_file(db_session, files[0].id)
    assert (kept.title, kept.creator, kept.license, kept.source_model_id) == (
        "Cable Clip",
        "Jane Doe",
        "CC-BY-4.0",
        "x7q3k2pdbm4z",
    )
    await library_import_meta.forget_files(db_session, [files[0].id])
    left = (await db_session.execute(select(LibraryFileImportMeta.library_file_id))).scalars().all()
    assert left == [files[1].id]
