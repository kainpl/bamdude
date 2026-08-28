"""A migration that cannot even be called is a migration that fails on restart.

A label migration called ``add_column(conn, table, name, type)`` — four
arguments to a three-argument helper, because the column name and its type go in
one string. Nothing caught it: the integration database builds its schema with
``create_all()`` and marks migrations applied, so no test ever executed that
line. It surfaced as an app that died mid-boot on a real installation, on
"Applying migration ...", with the traceback going to a console nobody was
watching. (That migration has since been folded into the one it amended, so the
call is not in the tree to look at — the guard is.)

Reading the migration files rather than running them is the point: it costs
nothing, needs no database, and covers every migration including the ones whose
branches a fixture would never reach.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.app.migrations import helpers

MIGRATIONS = sorted(Path(helpers.__file__).parent.glob("m[0-9]*.py"))

#: Every helper a migration calls positionally. ``conn`` counts as an argument,
#: which is why these are checked against the real signature rather than a
#: hand-written number that would drift the same way the call did.
CHECKED = ("add_column", "column_exists", "table_exists", "recreate_table", "drop_column")


def _arity(name: str) -> tuple[int, int]:
    """(minimum, maximum) positional arguments the helper accepts."""
    signature = inspect.signature(getattr(helpers, name))
    positional = [p for p in signature.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    required = sum(1 for p in positional if p.default is p.empty)
    return required, len(positional)


def _calls(path: Path) -> list[tuple[str, int, int]]:
    """(helper, line, positional argument count) for each checked call."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in CHECKED:
            found.append((name, node.lineno, len(node.args)))
    return found


def test_there_are_migrations_to_check():
    """⚠️ A glob that matches nothing passes every parametrised test below."""
    assert len(MIGRATIONS) > 100, f"only found {len(MIGRATIONS)} migrations"


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.stem)
def test_every_helper_call_matches_the_helper(path: Path):
    for name, line, given in _calls(path):
        low, high = _arity(name)
        assert low <= given <= high, (
            f"{path.name}:{line} calls {name}() with {given} positional arguments; "
            f"it takes {low}..{high}. This does not fail until the migration actually runs, "
            f"which is on somebody's install."
        )


#: Migrations that already shipped doing this. They are frozen — a released
#: migration is never edited — so they are recorded rather than fixed.
#:
#: ⚠️ **This list must not grow.** Every name on it is a migration that would
#: break if a later one added a column to the same table, which is a bet that
#: happened to pay off, not a pattern to copy.
GRANDFATHERED = {
    "m003_enforce_admin_user",
    "m036_library_file_tags",
    "m037_project_geometry_tags",
    "m046_normalize_group_permissions",
    "m057_archive_bed_type",
    "m059_stock_forecasting",
    "m091_ownership_read_split_backfill",
    "m102_slicer_pipelines",
    "m111_makerworld_permission_backfill",
    "m112_drop_pipeline_runs",
    "m123_zigbee_device_settings",
    "m137_sliced_by_content_backfill",
    "m138_repair_ams_sync_full_spool_writes",
    "m145_users_read_slim_permission",
    "m146_label_templates",
    "m147_device_direct_labels",
}


def test_the_grandfathered_list_is_all_real_migrations():
    """A typo here silently exempts nothing and hides nothing — but it also
    means the name it was meant to cover is unprotected."""
    stems = {path.stem for path in MIGRATIONS}
    assert stems >= GRANDFATHERED, GRANDFATHERED - stems


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.stem)
def test_a_seed_does_not_touch_a_mapped_class_whole(path: Path):
    """⚠️ ``update(Model)`` / ``select(Model)`` in a seed is the mid-chain break.

    The mapped class carries whatever the CURRENT code declares, including
    columns a LATER migration adds. An entity-wide statement in a seed can
    therefore name a column the database being upgraded does not have yet — and
    the chain dies partway, on an install, leaving the schema between two
    versions. Seeds name their columns.
    """
    if path.stem in GRANDFATHERED:
        pytest.skip("shipped before the rule and frozen — see GRANDFATHERED")

    tree = ast.parse(path.read_text(encoding="utf-8"))
    seed = next(
        (n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "seed"),
        None,
    )
    if seed is None:
        pytest.skip("no seed")

    for node in ast.walk(seed):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) not in ("select", "update", "delete"):
            continue
        for argument in node.args:
            # A bare Name that is Capitalised is a mapped class; anything
            # subscripted or attributed (Model.column, table.c.x) is fine.
            if isinstance(argument, ast.Name) and argument.id[:1].isupper():
                pytest.fail(
                    f"{path.name}:{node.lineno} passes the whole mapped class {argument.id} to "
                    f"{node.func.id}(). Name the columns instead — see this test's docstring."
                )
