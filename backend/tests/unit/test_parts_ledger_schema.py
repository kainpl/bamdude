"""The parts-ledger tables exist in metadata with the agreed shape."""

from sqlalchemy import inspect

from backend.app.core.database import Base
from backend.app.migrations import m158_parts_ledger
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.project_part import ProjectPart


def test_print_archive_parts_columns():
    cols = {c.name for c in inspect(PrintArchivePart).columns}
    assert cols == {"id", "archive_id", "name", "name_key", "identify_ids", "quantity", "defective"}


def test_project_parts_columns_and_unique_key():
    cols = {c.name for c in inspect(ProjectPart).columns}
    assert cols == {"id", "project_id", "name", "name_key", "target_qty"}
    uniques = {tuple(u.columns.keys()) for u in ProjectPart.__table__.constraints if u.name == "uq_project_parts_key"}
    assert ("project_id", "name_key") in uniques


def test_both_tables_are_registered_with_base():
    assert "print_archive_parts" in Base.metadata.tables
    assert "project_parts" in Base.metadata.tables


def test_migration_header():
    assert m158_parts_ledger.version == 158
    assert m158_parts_ledger.name == "parts_ledger"
