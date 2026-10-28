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
import tempfile
from hashlib import sha256
from pathlib import Path

import pytest

pytestmark = pytest.mark.postgres

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER = "backend.tests.integration.postgres_scenario_runner"


@pytest.fixture(scope="session", autouse=True)
def _serialize_shared_postgres_target():
    """Keep xdist workers from wiping the same declared scratch DB at once.

    Individual scenarios still exercise independent sessions/processes inside
    their own runner.  This only serializes the test module's deliberate
    ``_wipe`` calls when a developer supplies one ``TEST_POSTGRES_URL`` to
    several xdist workers.
    """

    url = os.environ.get("TEST_POSTGRES_URL")
    if not url or not os.environ.get("PYTEST_XDIST_WORKER"):
        yield
        return

    from filelock import FileLock

    digest = sha256(url.encode("utf-8")).hexdigest()[:16]
    lock = FileLock(str(Path(tempfile.gettempdir()) / f"bamdude-postgres-scenarios-{digest}.lock"))
    with lock.acquire(timeout=1_200):
        yield


def _pg_url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL not set — no PostgreSQL server to test against")
    return url


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def test_order_plan_publication_and_cancel_use_database_locks(tmp_path):
    env = {**os.environ, "DATA_DIR": str(tmp_path), "ORDER_PLAN_TEST_POSTGRES_URL": _async_url(_pg_url())}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/integration/test_order_plan_concurrency.py",
            "-q",
            "--timeout=45",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert result.returncode == 0, result.stdout[-12000:] + result.stderr[-3000:]


# This test owns a whole nested suite, not one transaction. Its outer watchdog
# must outlive subprocess.run's suite budget so that the child is reaped and its
# result is reported. Every child test still has its own 90-second deadline.
@pytest.mark.timeout(960)
def test_post_rebuild_transactions_on_postgres(tmp_path):
    """Dispatch attempts, interrupted dispatch and the tail journal on independent connections:
    no writer waits on a lock its own caller holds (post-rebuild D03/D08, I09, F09)."""
    url = _pg_url()
    _wipe(url)
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "DISPATCH_TEST_POSTGRES_URL": _async_url(url),
        "BAMDUDE_LOCK_ORDER_STRICT": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/integration/test_post_rebuild_on_postgres.py",
            "-q",
            "--timeout=90",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout[-20000:] + result.stderr[-4000:]


@pytest.mark.timeout(660)
def test_telemetry_day_window_on_postgres(tmp_path):
    """The telemetry day's bounds compared by PostgreSQL itself: the same boundary rows as SQLite,
    aware UTC bounds through asyncpg (telemetry-day-window T08)."""
    url = _pg_url()
    _wipe(url)
    env = {**os.environ, "DATA_DIR": str(tmp_path), "DISPATCH_TEST_POSTGRES_URL": _async_url(url)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/integration/test_telemetry_day_window_on_postgres.py",
            "-q",
            "--timeout=90",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-20000:] + result.stderr[-4000:]


# The suite runs every test under two server time zones; the outer watchdog outlives its budget.
@pytest.mark.timeout(960)
def test_plug_meter_watch_on_postgres(tmp_path):
    """The plug meter watch on PostgreSQL: the flush's source fence, the episode's partial unique index
    under a race, the archive merge under the advisory guard, rotations and policy changes from
    independent sessions -- under UTC and a DST zone -- and m211 on a database from before it
    (plug-meter-watchdog plan T9; A19, A26, A32)."""
    url = _pg_url()
    _wipe(url)
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "DISPATCH_TEST_POSTGRES_URL": _async_url(url),
        "BAMDUDE_LOCK_ORDER_STRICT": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/integration/test_plug_meter_watch_on_postgres.py",
            "-q",
            "--timeout=120",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout[-20000:] + result.stderr[-4000:]


@pytest.mark.timeout(960)
def test_rebalance_job_feasibility_on_postgres(tmp_path):
    """Rebalancing by job feasibility on PostgreSQL: the pass's printer and plate reads, the
    previous-success lookups and the final check after the target copy (rebalance-job-feasibility
    plan §5)."""
    url = _pg_url()
    _wipe(url)
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "DISPATCH_TEST_POSTGRES_URL": _async_url(url),
        "BAMDUDE_LOCK_ORDER_STRICT": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/integration/test_rebalance_job_feasibility_on_postgres.py",
            "-q",
            "--timeout=120",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout[-20000:] + result.stderr[-4000:]


@pytest.mark.timeout(420)
def test_queue_bulk_retry_concurrency(tmp_path):
    """The same real-runner matrix on independent PostgreSQL connections."""
    url = _pg_url()
    _wipe(url)
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "DISPATCH_TEST_POSTGRES_URL": _async_url(url),
        "BAMDUDE_LOCK_ORDER_STRICT": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/integration/test_queue_bulk_retry_concurrency.py",
            "-q",
            "--timeout=90",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=360,
    )
    assert result.returncode == 0, result.stdout[-20000:] + result.stderr[-4000:]


@pytest.mark.timeout(420)
def test_autoqueue_waiting_projection_and_notification_lifecycle(tmp_path):
    url = _pg_url()
    _wipe(url)
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "DISPATCH_TEST_POSTGRES_URL": _async_url(url),
        "BAMDUDE_LOCK_ORDER_STRICT": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/integration/test_auto_queue_actionable_notifications.py",
            "-q",
            "--timeout=90",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=360,
    )
    assert result.returncode == 0, result.stdout[-20000:] + result.stderr[-4000:]


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
    # The lock-order monitor raises in the scenarios too (WS-13 E1 BL2).
    env = {**os.environ, "DATA_DIR": str(data_dir), "BAMDUDE_LOCK_ORDER_STRICT": "1"}
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


class TestPrintStateRecovery:
    def test_recovery_and_autorepair_against_swap_writers_serialize(self, tmp_path_factory):
        pytest.importorskip("backend.app.services.swap_terminal", reason="requires the integrated T9 branch")
        url = _pg_url()
        _wipe(url)
        result = _run("print_recovery_swap", tmp_path_factory.mktemp("pg_recovery_swap"), url)
        assert result["first_before_second"], result
        for pair in ("recovery_vs_claim", "recovery_vs_refresh", "autorepair_vs_claim"):
            assert (result[pair]["first"], result[pair]["second"]) == ("ok", "ok"), (pair, result)
            assert result[pair]["waited"] >= 1, (pair, result)

    def test_the_runtime_repair_reacquires_its_guards_and_protects_a_new_start(self, tmp_path_factory):
        """Spec printer-runtime-stall r3 A16: two commits, the guards taken again after the first."""
        url = _pg_url()
        _wipe(url)
        result = _run("runtime_recovery", tmp_path_factory.mktemp("pg_runtime_recovery"), url)
        assert result == dict.fromkeys(
            ("new_start_sees_the_pause", "one_clear_per_token", "b_between_commits_untouched"), True
        )

    def test_operator_recovery_and_existing_writers_serialize(self, tmp_path_factory):
        url = _pg_url()
        _wipe(url)
        result = _run("print_state_recovery", tmp_path_factory.mktemp("pg_print_state_recovery"), url)
        assert result == dict.fromkeys(
            (
                "operator_vs_autorepair",
                "concurrent_idempotency",
                "late_foreign_reference",
                "archive_edit",
                "new_claim",
                "restore_and_id_reuse",
            ),
            True,
        )


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

    def test_the_part_stock_ledger_is_there_with_its_indexes(self, result):
        """m162's table and the three indexes its reads ride.

        The table alone is already covered by ``test_every_declared_table_exists``
        through ``Base.metadata``; the INDEXES are invisible from there. On a
        fresh install ``create_all`` builds them and m162's
        ``CREATE INDEX IF NOT EXISTS`` finds them already present — the branch
        that would show up as a duplicate index or an error only on a real
        server, which is what this file is for.
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

        indexes = set(asyncio.run(go()))
        wanted = {
            "ix_product_part_stock_movements_part_created",
            "ix_product_part_stock_movements_archive_id",
            "ix_product_part_stock_movements_project_line_id",
        }
        assert wanted <= indexes, f"missing {sorted(wanted - indexes)}; the table has {sorted(indexes)}"

    def test_the_hidden_hms_entries_table_has_its_unique_pair_and_index(self, result):
        """m163's table, with the constraint and the index the model also
        declares — ``create_all`` builds them on a fresh install and the
        migration's ``CREATE INDEX IF NOT EXISTS`` must find that one already
        there rather than trip over it; the constraint name is what makes a
        second Hide of the same entry idempotent at the storage level.
        """
        assert "hms_muted_entries" in result["tables"]

        psycopg = pytest.importorskip("asyncpg", reason="asyncpg is required to talk to PostgreSQL")
        import asyncio

        url = _pg_url()

        async def go() -> tuple[list[str], list[str]]:
            conn = await psycopg.connect(url, timeout=20)
            try:
                indexes = await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = $1",
                    "hms_muted_entries",
                )
                constraints = await conn.fetch(
                    "SELECT conname FROM pg_constraint WHERE conrelid = 'public.hms_muted_entries'::regclass"
                )
                return sorted(r["indexname"] for r in indexes), sorted(r["conname"] for r in constraints)
            finally:
                await conn.close()

        indexes, constraints = asyncio.run(go())
        assert "ix_hms_muted_entries_printer_id" in indexes, indexes
        assert "uq_hms_muted_printer_code" in constraints, constraints

    def test_the_printer_tag_tables_carry_their_widths_key_and_composite_pk(self, result):
        """m164's two tables, with the three things only a real server enforces.

        ``name_key`` is deliberately VARCHAR(128) against ``name``'s 64 because
        ``.lower()`` is not length-preserving in Unicode — SQLite ignores the
        width entirely, so a narrowed column here would reject a legal tag on
        PostgreSQL alone. The UNIQUE index is the case-insensitive identity the
        whole entity exists for, and the link table's composite primary key is
        what makes pinning the same tag twice a no-op at the storage level.
        """
        assert "printer_tags" in result["tables"]
        assert "printer_tag_links" in result["tables"]

        psycopg = pytest.importorskip("asyncpg", reason="asyncpg is required to talk to PostgreSQL")
        import asyncio

        url = _pg_url()

        async def go() -> tuple[list[str], dict[str, tuple[str, int | None]], list[str], list[str]]:
            conn = await psycopg.connect(url, timeout=20)
            try:
                indexes = await conn.fetch(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = $1 AND indexname = $2",
                    "printer_tags",
                    "ix_printer_tags_name_key",
                )
                columns = await conn.fetch(
                    "SELECT column_name, data_type, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = $1",
                    "printer_tags",
                )
                pk = await conn.fetch(
                    "SELECT a.attname FROM pg_constraint c "
                    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                    "WHERE c.conrelid = 'public.printer_tag_links'::regclass AND c.contype = 'p' "
                    # In declaration order: a (tag_id, printer_id) key would not serve a
                    # printer_id lookup, and membership alone cannot tell the two apart.
                    "ORDER BY array_position(c.conkey, a.attnum)"
                )
                fks = await conn.fetch(
                    "SELECT confrelid::regclass::text AS target FROM pg_constraint "
                    "WHERE conrelid = 'public.printer_tag_links'::regclass AND contype = 'f'"
                )
                return (
                    [r["indexdef"] for r in indexes],
                    {r["column_name"]: (r["data_type"], r["character_maximum_length"]) for r in columns},
                    [r["attname"] for r in pk],
                    sorted(r["target"] for r in fks),
                )
            finally:
                await conn.close()

        indexdefs, columns, pk_columns, fk_targets = asyncio.run(go())

        assert indexdefs, "ix_printer_tags_name_key is missing"
        assert "UNIQUE" in indexdefs[0], indexdefs[0]

        assert columns.get("name_key") == ("character varying", 128), columns.get("name_key")
        assert columns.get("name") == ("character varying", 64), columns.get("name")

        assert pk_columns == ["printer_id", "tag_id"], pk_columns
        # Both parents: without the FK to printer_tags a tag row could be deleted
        # out from under its links on PostgreSQL alone.
        assert fk_targets == ["printer_tags", "printers"], fk_targets


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


class TestCyrillicSearch:
    """Upper-case Cyrillic must find its lower-case row on a real server.

    The maintainer's dev database is a ``C``-locale one (measured 2026-09-12:
    PostgreSQL 18.4, ``datctype = C``), which is the shape that used to fail —
    ``lower('ЛАМПА')`` came back unchanged, so ``ILIKE`` matched nothing. The
    unit tests read the collated SQL as text; only a server says whether that
    text folds. Skipped with everything else here when ``TEST_POSTGRES_URL``
    is unset.
    """

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory) -> dict:
        url = _pg_url()
        _wipe(url)
        return _run("cyrillic_search", tmp_path_factory.mktemp("pg_cyrillic"), url)

    def test_capitals_find_the_lower_case_rows(self, result):
        assert result["products"] == ["Лампа настільна"]
        assert result["archives"] == ["Кронштейн"]

    def test_the_probe_reports_what_it_did(self, result):
        """Rows were found above, so a folding mechanism must be on record: the
        database folds natively, or the probe picked a collation. Neither is
        impossible — it would mean the search matched with nothing folding it,
        and the reported state is then not describing this run.

        This does not say WHICH path ran (a UTF-8-locale server legitimately
        answers "native"); the report itself names it, and the ``C``-locale dev
        server is what the maintainer runs this against.
        """
        assert result["native"] or result["collation"] in ("pg_c_utf8", "und-x-icu"), result

    def test_the_collated_sql_runs_on_this_server(self, result):
        """The collated SQL is sent to a server here even when this one folds
        natively — otherwise the branch would only ever be measured on the
        maintainer's ``C``-locale database, and CI (and every modern default
        install) would report the feature green without having compiled it once.

        The runner picks the collation the probe would have picked, sets the
        compiler's switch by hand and asks the same two searches again with a
        private compiled-SQL cache. A wrong rewrite does not reach this assertion
        at all — the server refuses the statement and the scenario fails.
        """
        if result["forced_collation"] is None:
            pytest.skip("this server offers no Unicode collation (pre-17 without ICU) — nothing to force")
        assert result["forced_collation"] in ("pg_c_utf8", "und-x-icu"), result
        assert result["forced_products"] == result["products"], result
        assert result["forced_archives"] == result["archives"], result


class TestArchiveWriteScope:
    """The cross-process half of archive_write_scope needs a real server."""

    def test_one_archive_writer_waits_until_the_other_commits(self, tmp_path_factory):
        url = _pg_url()
        _wipe(url)
        result = _run("archive_write_lock", tmp_path_factory.mktemp("pg_archive_write_lock"), url)
        assert result == {"blocked_before_commit": True, "second_entered": True}

    def test_shared_file_reference_scope_blocks_another_app_process(self, tmp_path_factory):
        """Attach donor reuse and delete share this PG-only cross-process fence."""
        url = _pg_url()
        _wipe(url)
        result = _run(
            "archive_file_reference_process_lock",
            tmp_path_factory.mktemp("pg_archive_file_reference_process_lock"),
            url,
        )
        assert result == {"blocked_before_release": True, "probe_entered_after_release": True}


class TestArchiveAttachRecovery:
    """Attach failure/retry is a real PostgreSQL transaction, not mocked SQL."""

    def test_rollback_marker_and_parallel_retry_keep_one_file(self, tmp_path_factory):
        url = _pg_url()
        _wipe(url)
        result = _run("archive_attach_recovery", tmp_path_factory.mktemp("pg_archive_attach_recovery"), url)
        assert result == {
            "failed": True,
            "marked": True,
            "recovered": True,
            "marker_cleared": True,
            "concurrent": [True, True],
            "concurrent_has_file": True,
            "archive_files": 1,
        }


class TestPrinterLaneAdmission:
    """The shared queue guard must work across real PostgreSQL sessions."""

    def test_one_direct_start_wins_and_the_other_reads_the_active_claim(self, tmp_path_factory):
        url = _pg_url()
        _wipe(url)
        result = _run("printer_lane_admission", tmp_path_factory.mktemp("pg_printer_lane_admission"), url)
        assert result == {
            "blocked_before_commit": True,
            "second_entered": True,
            "second_code": "active_claim",
        }


class TestPrintAnalysisConnectionRelease:
    """The cache wait must not hold a PostgreSQL transaction or pool checkout."""

    def test_a_cold_projection_releases_its_transaction_before_waiting(self, tmp_path_factory):
        url = _pg_url()
        _wipe(url)
        result = _run("analysis_wait_release", tmp_path_factory.mktemp("pg_analysis_wait"), url)
        assert result == {
            "reader_transaction_open_while_waiting": False,
            "writer_completed": True,
            "projection_active": True,
        }


class DeadlockReproduced(Exception):
    """PostgreSQL answered SQLSTATE 40P01 — the lock cycle the test exists to catch.

    The ONLY failure the reproduction's ``xfail`` accepts: a scenario that did not
    reach both locks, a timeout, a 409/422 from the data or another SQLSTATE stay
    red (spec WS-13 E1, R12)."""


class TestLineDoorLockOrder:
    """Q16 (WS-13 E1, T1b): a line door locked the line before its position while the
    issue dialog locks the position before the line."""

    def test_a_quantity_change_and_an_issue_of_one_line_never_deadlock(self, tmp_path_factory):
        url = _pg_url()
        _wipe(url)
        result = _run("line_door_deadlock", tmp_path_factory.mktemp("pg_line_door_deadlock"), url)
        if "deadlock" in (result["a"], result["b"]):
            raise DeadlockReproduced(json.dumps(result, ensure_ascii=False))
        # Both stopped inside their operations — or B was seen waiting on A's lock.
        assert result["which"] in ("barrier", "waiting"), result
        assert (result["a"], result["b"]) == ("ok", "ok"), result
        # The quantity change takes the position before the line (spec BL6)…
        touched = [table for _kind, table, _nowait in result["a_sql"] if table in ("stock_items", "project_lines")]
        assert touched and touched[0] == "stock_items", result["a_sql"]
        # …so the issue waited on it instead of forming a cycle.
        assert result["which"] == "waiting", result
        assert result["consistent"], result

    def test_a_first_stock_position_waits_for_the_quantity_edits_product_gate(self, tmp_path_factory):
        """BL6: position creation cannot change the edit's lock set behind its gate.
        PostgreSQL must show C waiting; both actual operations then commit."""
        url = _pg_url()
        _wipe(url)
        result = _run("line_set_changed", tmp_path_factory.mktemp("pg_line_set_changed"), url)
        assert (result["a"], result["c"], result["which"]) == ("ok", "ok", "waiting"), result
        assert result["quantity"] == 4, result
        assert result["on_hand"] == 2, result
        assert result["movements_by_line"] == 0, result
        assert result["events"] == 1, result

    def test_deleting_an_order_another_door_is_writing_to_refuses_at_once(self, tmp_path_factory):
        """BL6: the order row goes last and FOR UPDATE NOWAIT — the delete never waits
        on a door that merely references the order."""
        url = _pg_url()
        _wipe(url)
        result = _run("order_delete_busy", tmp_path_factory.mktemp("pg_order_delete_busy"), url)
        assert result["b"].startswith("http:409:"), result
        assert "being changed right now" in result["b"], result
        assert result["order_exists"] is True, result
        assert result["lines"] == 1, result


def _protocol(tmp_path_factory, name: str) -> dict:
    url = _pg_url()
    _wipe(url)
    return _run(f"protocol:{name}", tmp_path_factory.mktemp(f"pg_protocol_{name}"), url)


def _no_crash(outcome: str) -> None:
    assert outcome != "deadlock" and not outcome.startswith("error:"), outcome


class TestLockProtocol:
    """WS-13 E1, spec BL / T1 scenarios a–i: the product gate, NOWAIT footprints and the
    lock ledger under two real PostgreSQL sessions, each stopped inside its operation."""

    def test_a_group_added_while_the_issue_dialog_creates_the_first_position(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "a")
        assert r["which"] == "waiting", r  # B waited at the gate A holds — no cycle to form
        assert (r["a"], r["b"]) == ("ok", "ok"), r
        assert r["positions"] == 1 and r["position_choices"] == 2, r  # Colour + the new Size
        assert r["line_groups"] == 2 and r["line_key_ok"] and r["consistent"], r

    def test_a_part_rebound_while_the_issue_dialog_creates_the_first_position(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "b")
        assert r["which"] == "waiting", r
        assert (r["a"], r["b"]) == ("ok", "ok"), r
        assert r["line_key_ok"] and r["consistent"], r

    def test_completing_through_patch_against_completing_in_the_issue_dialog(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "c")
        assert r["which"] == "waiting", r
        assert r["a"] == "ok", r
        assert r["b"].startswith("http:409:") and "active order" in r["b"], r
        assert r["status"] == "completed", r

    def test_an_order_copy_against_a_new_line_of_the_same_product(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "d")
        assert r["which"] == "waiting", r
        assert (r["a"], r["b"]) == ("ok", "ok"), r
        assert r["lines"] == 3 and r["every_line_has_its_choices"], r

    def test_deleting_a_one_off_products_last_line_against_configuring_it(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "e")
        assert r["which"] == "waiting", r
        assert r["a"] == "ok", r
        assert r["b"].startswith("http:404:"), r
        assert r["product_left"] == 0, r

    def test_configuration_writers_stay_fresh_behind_the_gate(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "f")
        for pair in ("group_then_configuration", "configuration_then_group", "group_then_new_line"):
            assert r[pair]["which"] == "waiting", (pair, r[pair])
            assert (r[pair]["a"], r[pair]["b"]) == ("ok", "ok"), (pair, r[pair])
        g = r["group_then_configuration"]
        assert g["has_size"] and g["kept_blue"] and g["key_ok"] and g["kit_unchanged_by_a_new_standard"], g
        c = r["configuration_then_group"]
        assert c["has_size"] and c["kept_blue"] and c["key_ok"], c
        assert r["group_then_new_line"]["new_line_has_size"], r["group_then_new_line"]
        won = r["choice_then_delete"]
        assert won["a"] == "ok" and won["b"].startswith("http:409:") and "chosen in" in won["b"], won
        assert won["line_keeps_blue"] is not None, won
        lost = r["delete_then_choice"]
        assert lost["a"] == "ok" and lost["b"].startswith("http:422:"), lost
        assert lost["dangling"] == 0, lost

    def test_every_class_of_every_footprint_answers_busy_at_once(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "g")
        after = r.pop("after")
        for case, seen in r.items():
            assert seen["outcome"].startswith("http:409:") and "product_busy" in seen["outcome"], (case, seen)
            assert seen["seconds"] < 2, (case, seen)
        # Every refused door rolled back whole.
        assert after == {"groups": 1, "base_exists": 1, "doomed_exists": 1}, after

    def test_the_ledger_follows_savepoints_and_the_real_caller(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "i")
        assert r["released_inside_savepoint"] and r["relocked_after_restore"], r
        assert r["delete"] == "ok", r
        assert r["gate_statements"] == 1, r  # the cascade re-entered the gate without SQL
        assert r["product_left"] == 0, r
        assert (r["first_credit"], r["second_credit"]) == (1, 0), r

    def test_the_variants_draft_under_two_sessions(self, tmp_path_factory):
        """T2: two drafts of one revision — exactly one wins; a single route first makes an
        apply opened before it stale; a new group lands on the first position and on a
        reconfigured line."""
        r = _protocol(tmp_path_factory, "t2")
        for pair in ("apply_vs_apply", "route_vs_apply"):
            assert r[pair]["which"] == "waiting", r[pair]
            assert r[pair]["a"] == "ok", r[pair]
            assert r[pair]["b"].startswith("http:409:") and "variants_changed" in r[pair]["b"], r[pair]
        first = r["apply_group_vs_first_position"]
        assert first["which"] == "waiting" and (first["a"], first["b"]) == ("ok", "ok"), first
        assert first["line_groups"] == 2, first
        conf = r["apply_group_vs_configuration"]
        assert conf["which"] == "waiting" and (conf["a"], conf["b"]) == ("ok", "ok"), conf
        assert conf["line_groups"] == 2 and conf["kept_blue"] and conf["key_ok"], conf

    def test_a_part_born_bound_wins_against_deleting_its_option(self, tmp_path_factory):
        """WS-13 E10 R10: the create holds the gate; the delete WAITS (no 409 product_busy —
        the gate is not a NOWAIT footprint), then sees the bound part and refuses."""
        r = _protocol(tmp_path_factory, "bound_part_first")
        assert r["which"] == "waiting", r
        assert r["a"] == "ok", r
        assert r["b"].startswith("http:409:") and "bound to this option" in r["b"], r
        assert r["option_exists"] and r["part"]["variant_option_id"] is not None, r

    def test_deleting_an_option_first_refuses_the_part_born_bound_to_it(self, tmp_path_factory):
        """WS-13 E10 R10: the delete holds the gate; the create WAITS, then reads the
        option behind the gate — gone: 422, and neither the part nor its aliases exist."""
        r = _protocol(tmp_path_factory, "option_delete_first")
        assert r["which"] == "waiting", r
        assert r["a"] == "ok", r
        assert r["b"].startswith("http:422:"), r
        assert not r["option_exists"] and r["part"] is None, r

    def test_a_prints_exits_lock_the_order_before_the_print(self, tmp_path_factory):
        """WS-13 E13 V04 (Codex round 2): the archive editor and the trash take the order and
        then the print, as add-/remove-archives do — B waits on the order A holds; no
        crossing ends in a deadlock, and the print ends where the later door put it."""
        r = _protocol(tmp_path_factory, "print_exits")
        for crossing in ("remove_then_patch", "patch_then_remove", "add_then_trash", "trash_then_add"):
            c = r[crossing]
            assert c["which"] == "waiting", (crossing, c)
            _no_crash(c["a"])
            _no_crash(c["b"])
            assert c["a"] == "ok", (crossing, c)
        assert r["remove_then_patch"]["b"] == "ok" and r["remove_then_patch"]["project"] is None, r
        assert r["patch_then_remove"]["b"] == "ok" and r["patch_then_remove"]["project"] is None, r
        # The trash read the print's order before its lock; the batch moved it meanwhile —
        # «changed, try again», never the print trashed out of an order it did not lock.
        moved = r["add_then_trash"]
        assert moved["b"] == "ok" or moved["b"].startswith("http:409:"), moved
        assert moved["project"] == r["other"], moved
        # The trash won: the batch, under the print's lock, finds it in the trash and files
        # nothing (WS-13 E13 V08) — both ended (no deadlock), the print keeps its order.
        won = r["trash_then_add"]
        assert won["b"].startswith("http:409:") and "print_in_trash" in won["b"], won
        assert won["trashed"] and won["project"] == r["order"], won


class TestProductPageReads:
    """WS-13 E9 A01–A03 on PostgreSQL (E13 K02): the product page's counters, its files and
    folders, and the orders list's lines of the product — until now proven on SQLite only."""

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory) -> dict:
        url = _pg_url()
        _wipe(url)
        return _run("e9_reads", tmp_path_factory.mktemp("pg_e9_reads"), url)

    def test_the_card_counts_documents_and_distinct_orders(self, result):
        assert result["documents_count"] == 2, result  # the picture is not a document
        assert result["orders_count"] == 2, result  # two orders hold its lines, one of them twice

    def test_the_files_say_3mf_case_aside_and_whether_their_folder_is_linked(self, result):
        files = {int(k): v for k, v in result["files"].items()}
        assert files[result["boxed"]] == {"is_3mf": True, "in_linked_folder": True}, result
        assert files[result["loose"]] == {"is_3mf": False, "in_linked_folder": False}, result
        assert [f["folder_id"] for f in result["folders"]] == [result["folder"]], result

    def test_the_orders_list_carries_each_orders_lines_of_the_product(self, result):
        lines = {int(k): v for k, v in result["product_lines"].items()}
        first, second, third = result["orders"]
        assert lines[first] == [["product", 2], ["product", 3]], result
        assert lines[second] == [["product", 4]], result
        assert lines.get(third, []) == [], result


class TestStockJournalPages:
    """WS-13 E1 T9 / ST1: the journal's numbered pages on a real PostgreSQL."""

    def test_the_union_of_both_books_pages_as_the_cursor_does(self, tmp_path_factory):
        r = _protocol(tmp_path_factory, "journal")
        # shop() seeds 10 of each part (2 parts rows); 7 receipts, 7 more parts moves.
        assert r["same_as_cursor"] and r["asc_is_reversed"], r
        assert r["rows"] == r["unique"] == r["total"] == 7 + 7 + 2, r
        assert r["parts_total"] == 9, r
        assert r["products"] == ["Protocol lamp"], r


class TestStaleReadsBehindTheGates:
    """WS-13 E1 implementation review, round 1 (V01–V03): another session's writer commits
    while this request waits; what the request writes or answers must describe the state
    that writer left — two real PostgreSQL sessions, real writers, nothing mocked but the
    pause."""

    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory) -> dict:
        return _protocol(tmp_path_factory, "stale_reads")

    def test_an_unchanged_draft_from_an_older_snapshot_is_refused(self, result):
        r = result["revision"]
        assert r["groups_seen"] == 0, r
        assert r["outcome"] == "http:409:variants_changed", r
        assert r["groups_left"] == 1, r  # the group nobody saw survives

    def test_a_rebinding_writes_what_was_asked_and_keeps_saved_kits(self, result):
        r = result["binding"]
        assert r["answered"] == r["stored"] == r["requested"], r
        assert r["kit_after"] == r["kit_before"], r  # the saved line's kit never moved

    @pytest.mark.parametrize("case", ["customer", "customer_removed"])
    def test_an_issue_never_goes_to_a_customer_the_order_no_longer_has(self, result, case):
        r = result[case]
        assert r["outcome"].startswith("refused:409:"), r
        assert (r["issues"], r["movements_moved"]) == (0, 0), r
        assert r["customer_now"] == r["customer_wanted"], r


def _swap(tmp_path_factory, name: str) -> dict:
    url = _pg_url()
    _wipe(url)
    return _run(f"swap:{name}", tmp_path_factory.mktemp(f"pg_swap_{name}"), url)


class TestSwapClaim:
    """Our owed change_table under two real PostgreSQL sessions (Codex review r1, SS-I-R1..R3): one durable claim
    committed before the command, the exact queue owner, every error held -- with the process-local locks off, so
    PostgreSQL's advisory and row locks are what serialize the sessions."""

    def test_two_connect_edges_send_the_command_once(self, tmp_path_factory):
        r = _swap(tmp_path_factory, "two_edges")
        assert r["outcomes"] == ["ok", "ok"] and all(o == "ok" for o in r["resolvers"]), r
        assert r["sent"] == 1, r
        assert r["seen_before_send"] == ["attempted"], r  # a third session saw the committed claim first
        assert (r["stage"], r["pending"], r["queue"]) == ("finished", False, "idle"), r
        assert (r["usage"], r["energy"]) == (1, 1), r  # the second edge's stale copy closed nothing
        assert r["max_waiting"] >= 1, r  # the second close waited on the first's row lock

    def test_a_claim_taken_by_a_new_job_first_sends_nothing(self, tmp_path_factory):
        r = _swap(tmp_path_factory, "new_owner_before_the_claim")
        assert r["outcomes"] == ["ok", "ok"], r
        assert r["waited"] >= 1, r  # the claim waited on the dispatch lane PostgreSQL held
        assert r["sent"] == 0, r
        assert (r["queue"], r["current"]) == ("printing", r["new_job"]), r
        assert (r["stage"], r["hold"]) == ("owed", "swap_hold_run_unproven"), r

    def test_a_claim_lost_during_the_command_is_left_to_the_new_job(self, tmp_path_factory):
        r = _swap(tmp_path_factory, "new_owner_during_the_command")
        assert r["outcome"] == "ok" and r["sent"] == 1, r
        assert (r["queue"], r["current"]) == ("printing", r["new_job"]), r
        assert r["stage"] == "finished" and r["run_kept"], r

    def test_an_interrupted_command_is_held_at_the_next_connect_never_repeated(self, tmp_path_factory):
        r = _swap(tmp_path_factory, "interrupted_then_reconnect")
        assert r["first"] == "cancelled" and r["edge"] == "ok", r
        assert r["sent"] == 1, r
        assert (r["stage"], r["pending"], r["hold"], r["queue"]) == (
            "attempted",
            True,
            "swap_hold_attempt_unknown",
            "paused",
        ), r
        assert [r["usage"], r["energy"]] == r["effects_before"], r  # nothing booked again
        assert r["run_kept"], r

    def test_an_interrupted_command_leaves_another_jobs_queue_alone(self, tmp_path_factory):
        r = _swap(tmp_path_factory, "interrupted_then_another_job")
        assert r["first"] == "cancelled" and r["edge"] == "ok", r
        assert r["sent"] == 1, r
        assert (r["queue"], r["current"]) == ("printing", r["new_job"]), r
        assert r["stage"] == "attempted" and r["run_kept"], r

    def test_a_lookup_error_ends_in_an_explained_hold(self, tmp_path_factory):
        r = _swap(tmp_path_factory, "lookup_error")
        assert r["outcome"] == "ok" and r["sent"] == 0, r
        assert (r["hold"], r["queue"], r["pending"]) == ("swap_hold_macro_error", "paused", True), r

    def test_the_claim_an_archive_write_and_an_admission_never_deadlock(self, tmp_path_factory):
        r = _swap(tmp_path_factory, "no_deadlock")
        assert r["outcomes"] == ["ok", "ok", "ok"], r
        assert r["waited"] >= 2, r  # both waited on PostgreSQL behind the open claim
        assert r["admission"] == ["active_claim"], r
        assert r["writer_key"] and r["stage"] == "finished" and r["sent"] == 1, r
        assert r["queue"] == "idle", r


def _declare(tmp_path_factory, name: str) -> dict:
    url = _pg_url()
    _wipe(url)
    return _run(f"declare:{name}", tmp_path_factory.mktemp(f"pg_declare_{name}"), url)


class TestSwapDeclaration:
    """The operator's swap declaration under two real PostgreSQL sessions (T9 plan, scenarios 5-9): each writer holds
    its lock until PostgreSQL shows the other waiting, then lets go -- with the process-local locks off."""

    def test_two_declarations_of_one_revision_commit_once(self, tmp_path_factory):
        r = _declare(tmp_path_factory, "two_declares")
        for kind in ("archive", "library"):
            k = r[kind]
            assert k["outcomes"] == ["409:swap_source_changed", "ok"], (kind, r)
            assert k["waited"] >= 1 and k["audit"] == ["declare"], (kind, r)
            assert k["origin"] == "operator" and k["effective"] in (True, False), (kind, r)

    def test_a_scan_and_a_declaration_of_one_row_never_interleave(self, tmp_path_factory):
        r = _declare(tmp_path_factory, "scan_vs_declare")
        new = r["declaration_first_new_bytes"]
        assert (new["declared"], new["scanned"]) == ("ok", "ok") and new["waited"] >= 1, r
        assert (new["origin"], new["effective"], new["swap_tag"]) == ("classifier", False, False), r
        same = r["declaration_first_same_bytes"]
        assert (same["declared"], same["scanned"]) == ("ok", "ok") and same["waited"] >= 1, r
        assert (same["origin"], same["effective"], same["swap_tag"]) == ("operator", True, True), r
        late = r["scan_first_new_bytes"]
        assert (late["declared"], late["scanned"]) == ("409:swap_source_changed", "ok") and late["waited"] >= 1, r
        assert (late["origin"], late["effective"], late["audit"]) == ("classifier", False, []), r

    def test_a_refresh_and_an_assignment_never_mix_their_answers(self, tmp_path_factory):
        r = _declare(tmp_path_factory, "refresh_vs_assignment")
        first = r["refresh_first"]
        assert (first["refreshed"], first["assigned"]) == ("ok", "conflict:swap_source_changed"), r
        assert first["waited"] >= 1 and first["children"] == [], r
        assert (first["queue_fact"], first["auto_fact"], first["auto_status"]) == (True, True, "pending"), r
        assert first["audit"] == ["declare", "refresh_jobs"], r
        claimed = r["assignment_first"]
        assert (claimed["assigned"], claimed["refreshed"]) == ("ok", "409:jobs_changed"), r
        assert claimed["waited"] >= 1 and claimed["children"] == [None], r
        assert (claimed["queue_fact"], claimed["auto_status"], claimed["audit"]) == (None, "assigned", ["declare"]), r

    def test_a_refresh_refuses_whole_when_a_job_or_its_source_moved(self, tmp_path_factory):
        r = _declare(tmp_path_factory, "refresh_vs_changes")
        move = r["move"]
        assert (move["changed"], move["refreshed"]) == ("ok", "409:jobs_changed") and move["waited"] >= 1, r
        assert (move["facts"], move["audit"]) == ([None, None], ["declare"]), r
        delete = r["delete"]
        assert (delete["changed"], delete["refreshed"]) == ("ok", "409:job_not_refreshable"), r
        assert delete["waited"] >= 1 and (delete["facts"], delete["audit"]) == ([None], ["declare"]), r
        redeclare = r["redeclare"]
        assert (redeclare["redeclared"], redeclare["refreshed"]) == ("ok", "409:source_changed"), r
        assert redeclare["waited"] >= 1 and redeclare["facts"] == [None], r
        assert redeclare["audit"] == ["declare", "declare"], r
        assert r["restore"]["refreshed"] == "409:source_changed" and r["restore"]["facts"] == [None], r
        free = r["no_deadlock"]
        assert free["refreshed"] in ("ok", "409:source_changed"), r
        assert (free["declared"], free["claimed"]) == ("ok", "ok") and free["waited"] >= 1, r

    def test_a_refresh_and_a_hard_delete_of_its_source_never_deadlock(self, tmp_path_factory):
        r = _declare(tmp_path_factory, "refresh_vs_source_delete")
        assert r["deleted"] == "ok" and r["waited"] >= 1, r
        assert r["refreshed"].split(":")[0] in ("404", "409"), r  # the source is gone; never a deadlock or an error
        # The declaration's audit line outlives its source (spec r3 §9); no refresh line was written.
        assert r["facts"] == [None, None] and r["audit"] == ["declare"], r

    def test_a_key_losing_its_authority_while_the_refresh_waits_changes_nothing(self, tmp_path_factory):
        r = _declare(tmp_path_factory, "key_revoked_while_waiting")
        scope, read = r["printer_scope"], r["queue_read"]
        assert scope["refreshed"] == "409:job_not_refreshable" and scope["waited"] >= 1, r
        assert read["refreshed"].startswith("403") and read["waited"] >= 1, r
        for k in (scope, read):
            assert (k["facts"], k["audit"]) == ([None], ["declare"]), r

    def test_one_operation_sent_twice_answers_once_and_a_reused_id_is_a_conflict(self, tmp_path_factory):
        r = _declare(tmp_path_factory, "one_operation_sent_twice")
        assert r["declare"] == {"outcomes": ["ok", "ok:replayed"], "lines": 1}, r
        assert r["refresh"] == {"outcomes": ["ok", "ok:replayed"], "lines": 1, "facts": [True]}, r
        race = r["declare_race"]
        assert race["outcomes"] == ["409:swap_declaration_conflict", "ok"] and race["lines"] == 1, r
        assert race["origins"] == ["None", "operator"], r  # the loser wrote nothing
        race = r["refresh_race"]
        assert race["outcomes"] == ["409:swap_refresh_conflict", "ok"] and race["lines"] == 1, r
        assert race["facts"] == [[None], [True]], r

    def test_the_upgrade_gives_every_job_its_own_incarnation_and_an_unwritten_audit_commits_nothing(
        self, tmp_path_factory
    ):
        r = _declare(tmp_path_factory, "upgrade_and_identity")
        assert (r["identities"], r["unique"], r["source_identity_columns"]) == (5, True, []), r
        assert (r["born"], r["clone_differs"]) == (36, True), r
        assert r["failed_declare"] == "error", r
        assert (r["after_failed"]["origin"], r["audit_after_failed"]) == (None, []), r
