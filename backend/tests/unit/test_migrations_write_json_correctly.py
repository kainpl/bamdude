"""No migration may assign plain text to a JSON column.

This class of fault has now shipped twice. m157 crash-looped a tester's
PostgreSQL install (2026-09-09) — *column "subscribed_events" is of type json
but expression is of type character varying* — and m022, m023 and m132 carried
the identical mistake, latent only because their backfill loops UPDATE solely
for rows that still need work.

It is invisible to the rest of the suite by construction: everything here runs
on SQLite, which has no opinion about types, and the model's ``Column(JSON)``
becomes a real ``json`` column only on PostgreSQL. Worse, the maintainer's own
upgrade path cannot catch it either — converting on SQLite first and importing
afterwards means the migration finds nothing left to convert.

So the guard is a source scan. Crude, and deliberately so: it fails on a
rewrite it should not care about, which costs one line here, while the fault it
prevents costs a user their install.

The sanctioned ways to write a JSON column are in ``helpers.as_json`` — a CAST
on PostgreSQL, bare on SQLite (a CAST there takes NUMERIC affinity and would
turn the document into 0).
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "backend" / "app" / "migrations"
MODELS = REPO / "backend" / "app" / "models"

# Assignments that need no cast: a literal NULL, an explicit PostgreSQL cast
# already written by hand, or SQLite's own json_* functions in a dialect branch.
_ALREADY_SAFE = ("as_json", "= NULL", "::json", "::jsonb", "json_remove", "json_set", "json_insert")


def _json_columns() -> set[str]:
    """Model fields declared ``Column(JSON)`` / ``mapped_column(JSON)``."""
    names: set[str] = set()
    for path in MODELS.rglob("*.py"):
        for match in re.finditer(
            r"^\s*(\w+)\s*(?::[^=]+)?=\s*(?:mapped_column|Column)\(\s*JSON",
            path.read_text(encoding="utf-8"),
            re.M,
        ):
            names.add(match.group(1))
    return names


def test_the_model_layer_actually_has_json_columns():
    """A guard that finds nothing to guard is not passing, it is broken."""
    columns = _json_columns()
    assert len(columns) > 10, f"only found {columns} — the model scan has drifted"
    assert {"subscribed_events", "file_metadata", "extra_data"} <= columns


def test_no_migration_assigns_plain_text_to_a_json_column():
    offenders: list[str] = []
    columns = _json_columns()

    for path in sorted(MIGRATIONS.glob("m*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for column in columns:
            for match in re.finditer(rf"SET\s+{re.escape(column)}\s*=", source, re.I):
                index = source[: match.start()].count("\n")
                statement = lines[index].strip()
                if not any(token in statement for token in _ALREADY_SAFE):
                    offenders.append(f"{path.name}:{index + 1}  {statement[:90]}")

    assert not offenders, (
        "these statements assign to a JSON column without a dialect-aware cast; "
        "wrap the expression in helpers.as_json():\n  " + "\n  ".join(offenders)
    )


def test_as_json_casts_only_on_postgresql(monkeypatch):
    """⚠️ Bare on SQLite is not an oversight. ``CAST('[\"a\"]' AS JSON)`` there
    takes NUMERIC affinity and yields 0 — the document would be destroyed."""
    from backend.app.migrations import helpers

    monkeypatch.setattr(helpers, "is_postgres", lambda: True)
    assert helpers.as_json(":m") == "CAST(:m AS JSON)"
    assert helpers.json_column_type() == "JSON"

    monkeypatch.setattr(helpers, "is_postgres", lambda: False)
    assert helpers.as_json(":m") == ":m"
    assert helpers.json_column_type() == "TEXT"


@pytest.mark.parametrize("name", ["m157_notifications_rework", "m022_label_object_metadata_backfill"])
def test_the_migrations_that_carried_the_fault_now_use_the_helper(name):
    """Pins the two the fault was actually found in, so a revert is loud."""
    source = (MIGRATIONS / f"{name}.py").read_text(encoding="utf-8")
    assert "as_json" in source, f"{name} no longer casts its JSON writes"


def test_every_migration_still_parses():
    """The scan above is textual; this makes sure the files it read are real."""
    for path in sorted(MIGRATIONS.glob("m*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
