"""m114 adds skip_objects_supported to both tables.

Asserts against the model tables directly, NOT via Base.metadata.create_all:
that needs the entire model graph imported (print_queue FKs auto_queue_items)
and fails on unrelated tables.
"""

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile


def test_library_files_has_column():
    col = LibraryFile.__table__.columns["skip_objects_supported"]
    assert col.nullable is False


def test_print_archives_has_column():
    col = PrintArchive.__table__.columns["skip_objects_supported"]
    assert col.nullable is False


def test_migration_metadata():
    from backend.app.migrations import m114_skip_objects_supported as m

    assert m.version == 114
    assert m.name == "skip_objects_supported"
    assert callable(m.upgrade)
    assert callable(m.seed)
