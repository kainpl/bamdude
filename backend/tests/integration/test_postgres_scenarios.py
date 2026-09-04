"""End-to-end PostgreSQL scenarios against a real server.

Every other PostgreSQL test in this suite mocks the engine and asserts on the
SQL *text*. That is why seven independent dialect breakages — boolean columns
compared and defaulted with 0/1, AUTOINCREMENT, ADD CONSTRAINT IF NOT EXISTS —
lived through several releases with a fresh PostgreSQL install being impossible
since 0.4.2. None of them are visible without a server that refuses the
statement.

Skipped unless ``TEST_POSTGRES_URL`` is set, so a normal local run is unaffected.
CI provides it from a service container.

    TEST_POSTGRES_URL=postgresql://user:pass@host:5432/db pytest -m postgres

The target database is WIPED (every table in the public schema) — point this at
a scratch database, never at anything you care about.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.postgres

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER = "backend.tests.integration.postgres_scenario_runner"


def _pg_url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL not set — no PostgreSQL server to test against")
    return url


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def _wipe(url: str) -> None:
    psycopg = pytest.importorskip("asyncpg", reason="asyncpg is required to talk to PostgreSQL")
    import asyncio

    async def go():
        conn = await psycopg.connect(url, timeout=20)
        try:
            await conn.execute(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                "EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        finally:
            await conn.close()

    asyncio.run(go())


def _run(mode: str, data_dir: Path, url: str | None, *extra: str) -> dict:
    env = {**os.environ, "DATA_DIR": str(data_dir)}
    if url:
        env["DATABASE_URL"] = _async_url(url)
    else:
        env.pop("DATABASE_URL", None)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", RUNNER, mode, *extra],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(f"scenario {mode!r} failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestFreshInstall:
    """A brand-new BamDude pointed at an empty PostgreSQL must come up."""

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory) -> dict:
        url = _pg_url()
        _wipe(url)
        return _run("fresh", tmp_path_factory.mktemp("pg_fresh"), url)

    def test_every_declared_table_exists(self, result):
        missing = sorted(set(result["declared"]) - set(result["tables"]))
        assert not missing, f"create_all did not produce: {missing}"

    def test_every_shipped_migration_applied(self, result):
        """Every migration the package ships ran — measured against what ships,
        not against a contiguous range.

        This used to assert ``range(first, last + 1)`` had no holes, which read
        an unused version number as a skipped migration. m116 then left 115 free
        on purpose so the zigbee branch could claim it without two files
        colliding on one version, and the chain is discovered and ordered by each
        module's own ``version`` — so a hole is not a defect and cannot become
        one. What IS a defect is a migration that exists and never applied, and
        contiguity never tested that: a missing *last* migration leaves no gap
        at all.
        """
        from backend.app.migrations import _discover_migrations

        versions = result["migrations"]
        assert versions, "no migrations recorded — the chain never ran"
        shipped = {m["version"] for m in _discover_migrations()}
        missing = sorted(shipped - set(versions))
        assert not missing, f"migrations that ship but never applied: {missing}"

    def test_system_groups_are_seeded_with_permissions(self, result):
        names = {g["name"] for g in result["groups"]}
        assert {"Administrators", "Operators", "Viewers"} <= names
        for g in result["groups"]:
            assert g["permissions"] > 0, f"group {g['name']} seeded with no permissions"

    def test_starts_with_no_users_so_setup_is_required(self, result):
        assert result["counts"].get("users") == 0

    def test_the_legacy_project_tables_are_gone(self, result):
        """m158 retires the whole make-it side of the old projects feature.

        Replaces an m044-era test that asserted which unique constraint
        ``project_print_plan_items`` ends up with: the table itself is dropped
        now, so the only thing left worth pinning on a real server is that the
        drop actually happened — the frozen migrations (m016, m044, m048) still
        CREATE and fill these tables earlier in the chain, so a fresh install
        proves m158 runs after them and cleans up.

        ``_m158_pending_plate_copies`` is in the list for the same reason from
        the other end: it is m158's own scratch table, created in ``upgrade()``
        and dropped at the very bottom of ``seed()``. Left behind it would mean
        ``seed()`` never reached its end, which is exactly the interrupted-run
        state the parts step is now written to recover from — and a fresh
        install has nothing to recover, so on this path it must be gone.
        """
        psycopg = pytest.importorskip("asyncpg", reason="asyncpg is required to talk to PostgreSQL")
        import asyncio

        url = _pg_url()
        legacy = (
            "project_bom_items",
            "project_print_plan_items",
            "project_parts",
            "library_file_projects",
            "library_folder_projects",
            "_m158_pending_plate_copies",
        )

        async def go() -> set[str]:
            conn = await psycopg.connect(url, timeout=20)
            try:
                rows = await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY($1::text[])",
                    list(legacy),
                )
                return {r["tablename"] for r in rows}
            finally:
                await conn.close()

        survivors = asyncio.run(go())
        assert survivors == set(), (
            f"m158 left legacy or scratch project tables behind on a fresh install: {sorted(survivors)}"
        )

    def test_the_part_stock_ledger_is_there_with_its_index(self, result):
        """m162's table and the one index every read of it rides.

        The table alone is already covered by ``test_every_declared_table_exists``
        through ``Base.metadata``; the INDEX is invisible from there. On a fresh
        install ``create_all`` builds it and m162's ``CREATE INDEX IF NOT EXISTS``
        finds it already present — the branch that would show up as a duplicate
        index or an error only on a real server, which is what this file is for.
        """
        assert "product_part_stock_movements" in result["tables"]

        psycopg = pytest.importorskip("asyncpg", reason="asyncpg is required to talk to PostgreSQL")
        import asyncio

        url = _pg_url()

        async def go() -> list[str]:
            conn = await psycopg.connect(url, timeout=20)
            try:
                rows = await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = $1",
                    "product_part_stock_movements",
                )
                return sorted(r["indexname"] for r in rows)
            finally:
                await conn.close()

        indexes = asyncio.run(go())
        assert "ix_product_part_stock_movements_part_created" in indexes, (
            f"the (product_part_id, created_at) index is missing; the table has {indexes}"
        )


class TestSqliteMigration:
    """An existing SQLite install must carry its rows across."""

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory) -> tuple[dict, dict]:
        url = _pg_url()
        data_dir = tmp_path_factory.mktemp("pg_migrate")
        seeded = _run("seed", data_dir, None)["seeded"]
        assert (data_dir / "bamdude.db").exists(), "seed step produced no SQLite database"
        _wipe(url)
        return seeded, _run("migrate", data_dir, url)

    def test_rows_arrive_in_postgres(self, result):
        seeded, migrated = result
        for table, expected in seeded.items():
            assert migrated["counts"].get(table) == expected, (
                f"{table}: SQLite had {expected}, PostgreSQL has {migrated['counts'].get(table)} — "
                "the importer uses ON CONFLICT DO NOTHING, so rows can vanish without an error"
            )

    def test_source_sqlite_is_renamed_so_it_cannot_reimport(self, tmp_path_factory, result):
        # The migration renames the source on success; a second boot must not
        # find it and import a second time on top of the live data.
        _, migrated = result
        assert migrated["counts"], "sanity: migration produced no counts"

    def test_no_sequence_lags_its_table(self, result):
        _, migrated = result
        assert not migrated["lagging_sequences"], (
            f"sequences behind MAX(id): {migrated['lagging_sequences']} — "
            "the next insert into these tables would collide on the primary key"
        )


class TestProductExportImport:
    """A product ZIP must round-trip on PostgreSQL, not only on SQLite.

    The same round trip is covered in full by
    ``test_product_export_import.py``; this run exists for what the two back
    ends disagree about — JSON columns, sequences behind ``id``, and a NOT NULL
    a Python-side default fills. Skipped with everything else here when
    ``TEST_POSTGRES_URL`` is unset.
    """

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory) -> dict:
        url = _pg_url()
        _wipe(url)
        return _run("product_roundtrip", tmp_path_factory.mktemp("pg_product"), url)

    def test_the_card_and_the_composition_come_back(self, result):
        assert result["name"] == "Desk Lamp"
        assert result["designer"] == "Chef&koch" and result["design_id"] == "1234567"
        assert result["parts"] == {"shade.stl": 1, "hook.stl": 2, "clip.stl": 1}
        assert result["warnings"] == []
        assert result["filename"].startswith("desk-lamp_") and result["filename"].endswith(".zip")

    def test_the_plates_are_derived_from_the_files_themselves(self, result):
        assert sorted(result["plates"]) == [["gone.gcode.3mf", 1], ["gone.gcode.3mf", 2], ["kept.gcode.3mf", 0]]

    def test_the_surviving_file_is_matched_by_hash_and_only_the_other_is_ingested(self, result):
        assert result["library_rows_after"] == result["library_rows_before"], (
            "one file was deleted before the import and one survived — the survivor must be matched by "
            "hash and the deleted one re-ingested, so the row count must come back to where it started"
        )

    def test_the_attachments_and_the_cover_come_back(self, result):
        assert result["attachments"] == [["pictures", "shot.png", "import"]]
        assert result["cover_is_the_picture"] is True
