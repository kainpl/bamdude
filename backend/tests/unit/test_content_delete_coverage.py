"""C11: literal policies and caller coverage must change with the implementation."""

import json
from pathlib import Path

from sqlalchemy import JSON, Integer

from backend.app.core.database import Base, import_all_models
from backend.app.models.library import LibraryFolder
from backend.app.models.printer import Printer
from backend.tests.content_delete_inventory import application_sources, census

APP = Path(__file__).parents[2] / "app"
FIXTURE = Path(__file__).parents[1] / "fixtures/content_delete_sites_v1.json"


def test_every_delete_and_primitive_caller_has_an_explicit_disposition():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = census(application_sources(APP))
    for kind, sites in actual.items():
        known = expected[kind]
        assert sites == [tuple(row["site"]) for row in known], (
            kind,
            "New or changed deletion/caller needs owner adjudication",
        )
        assert all(row["owner_policy"] for row in known)


def test_aliases_and_cascading_folder_doors_are_visible_to_the_census():
    example = {
        "new_door.py": """
from sqlalchemy import delete as drop
from backend.app.models.library import LibraryFile as File, LibraryFolder as Folder
async def bad(db, file_id, folder):
    remove = drop
    await db.execute(remove(File))
    await db.execute(drop(File).where(File.id == file_id))
    await db.execute(drop(Folder))
    await db.delete(folder)
    await db.execute("DELETE FROM library_files WHERE id = :id")
async def assignment_door(db):
    alias = drop
    await db.execute(alias(File))
"""
    }
    sites = census(example)["delete_sites"]
    assert any(callee == "sqlalchemy.delete" and target.endswith(".LibraryFile") for _, callee, target in sites)
    assert any(callee == "sqlalchemy.delete" and target.endswith(".LibraryFolder") for _, callee, target in sites)
    assert any(callee == "db.delete" and target == "folder" for _, callee, target in sites)
    assert any(callee == "raw SQL" for _, callee, _target in sites)
    assert any(
        owner.endswith("::assignment_door") and callee == "sqlalchemy.delete" for owner, callee, _target in sites
    )
    assert not LibraryFolder.__mapper__.relationships["files"].cascade.delete
    assert not Printer.__mapper__.relationships["archives"].cascade.delete


def test_all_36_user_edges_keep_the_reviewed_literal_policy():
    import_all_models()
    policy = json.loads((APP / "data/content_delete_user_reference_policy.json").read_text(encoding="utf-8"))
    actual = {}
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.target_fullname == "users.id":
                actual[f"{table.name}.{fk.parent.name}"] = (fk.parent.nullable, fk.ondelete)
    assert actual == {key: (row["nullable"], row["ondelete"]) for key, row in policy.items()}
    assert len(actual) == 36  # N56: library_folders.created_by_id


def test_all_112_parent_fk_edges_and_secondaries_have_a_literal_disposition():
    import_all_models()
    policy = json.loads((APP / "data/content_delete_fk_reference_policy.json").read_text(encoding="utf-8"))
    parents = {
        "printers",
        "print_archives",
        "projects",
        "library_files",
        "library_folders",
        "print_queue",
        "auto_queue_items",
        "users",
    }
    actual = {
        f"{table.name}.{fk.parent.name}": (fk.target_fullname, fk.parent.nullable, fk.ondelete)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name in parents
    }
    assert actual == {key: (row["target"], row["nullable"], row["ondelete"]) for key, row in policy.items()}
    # N56: library_folders.created_by_id; N62: group_printers.printer_id; N09: maintenance_history.printer_id
    assert len(actual) == 112
    assert all(row["policy"] for row in policy.values())


def test_numeric_and_json_carriers_have_no_unclassified_addition():
    import_all_models()
    numeric_policy = json.loads((APP / "data/content_delete_numeric_reference_policy.json").read_text(encoding="utf-8"))
    json_policy = json.loads((APP / "data/content_delete_json_reference_policy.json").read_text(encoding="utf-8"))
    numeric = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.c
        if column.name.endswith("_id") and isinstance(column.type, Integer) and not column.foreign_keys
    }
    carriers = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.c
        if isinstance(column.type, JSON)
    }
    assert numeric == set(numeric_policy)
    assert carriers == set(json_policy)
