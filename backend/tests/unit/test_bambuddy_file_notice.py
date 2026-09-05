"""An upstream Bambuddy database is reported on every start, and never consumed.

The import of a Bambuddy 2.2.2 database was removed on 2026-09-05. Two things
have to hold afterwards, and both are silent when broken:

* the operator who dropped such a file in is told why nothing happened — every
  start, because nothing renames or removes the file any more and a one-shot
  notice would scroll away years before they look; and
* no code path quietly eats it. The PostgreSQL auto-migrate is the one that
  could: it accepts a legacy *filename* and consumes what it accepts.

The notice therefore lives in ``run_all_migrations`` rather than in m000 — a
migration is recorded in ``_migrations`` and runs exactly once — and m000 itself
must now be provably inert. That the file survives a whole boot is the other
half, pinned by ``integration/test_legacy_bambuddy_file_survives_boot.py``.
"""

import logging
import sqlite3

import pytest

from backend.app.core.db_portable import _local_sqlite_candidate
from backend.app.migrations import _warn_if_foreign_bambuddy_file, m000_bambuddy_import

MIGRATIONS_LOGGER = "backend.app.migrations"
PORTABLE_LOGGER = "backend.app.core.db_portable"


def _sqlite_with(path, table: str) -> None:
    """A real SQLite file, so the ``telegram_chats`` probe reads it rather than erroring out."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def _expected(path) -> str:
    """Spelled out rather than formatted from the constant, so editing the constant fails here."""
    return (
        f"Found a Bambuddy database at {path}. Importing Bambuddy data was removed in 0.5.6 — "
        "BamDude and Bambuddy have diverged too far for a one-time import; the file is left "
        "untouched. See https://docs.bamdude.top/getting-started/upgrading/"
    )


def _messages(caplog, logger_name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


# ── The startup notice ────────────────────────────────────────────────────────


async def test_a_foreign_bambuddy_file_is_named_in_the_log(tmp_path, caplog):
    legacy = tmp_path / "bambuddy.db"
    _sqlite_with(legacy, "printers")

    with caplog.at_level(logging.WARNING, logger=MIGRATIONS_LOGGER):
        await _warn_if_foreign_bambuddy_file(tmp_path)

    assert _messages(caplog, MIGRATIONS_LOGGER) == [_expected(legacy)]
    assert legacy.exists(), "the notice must not consume the file it is only reporting"


async def test_the_notice_repeats_on_every_call(tmp_path, caplog):
    """The reason it is a startup check and not a migration.

    m000 is recorded in ``_migrations``, so a notice there fired once on a fresh
    install and stayed silent forever after — while the file sat in the data
    directory untouched, because the ``.bak`` rename went with the importer.
    """
    _sqlite_with(tmp_path / "bambuddy.db", "printers")

    with caplog.at_level(logging.WARNING, logger=MIGRATIONS_LOGGER):
        await _warn_if_foreign_bambuddy_file(tmp_path)
        await _warn_if_foreign_bambuddy_file(tmp_path)

    assert len(_messages(caplog, MIGRATIONS_LOGGER)) == 2


async def test_nothing_is_logged_when_no_legacy_database_is_present(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=MIGRATIONS_LOGGER):
        await _warn_if_foreign_bambuddy_file(tmp_path)

    assert _messages(caplog, MIGRATIONS_LOGGER) == []


async def test_our_own_301_database_is_not_reported_as_bambuddy(tmp_path, caplog):
    """A ``bambuddy.db`` written by BamDude 3.0.1 is ours, and already renamed by now.

    Uses the real ``telegram_chats`` probe rather than a patch, so the notice and
    ``_is_bamdude_301`` cannot drift apart into a state where every 3.0.1
    upgrader is told their own database was ignored.
    """
    _sqlite_with(tmp_path / "bambuddy.db", "telegram_chats")

    with caplog.at_level(logging.WARNING, logger=MIGRATIONS_LOGGER):
        await _warn_if_foreign_bambuddy_file(tmp_path)

    assert _messages(caplog, MIGRATIONS_LOGGER) == []


# ── m000 is inert ─────────────────────────────────────────────────────────────


async def test_m000_seed_does_nothing_and_says_nothing(tmp_path, caplog):
    """Whatever is in the data directory, the migration itself is a no-op.

    If the notice ever migrates back into m000 it starts firing once per install
    again, which is the bug this arrangement exists to prevent.
    """
    legacy = tmp_path / "bambuddy.db"
    _sqlite_with(legacy, "printers")

    with caplog.at_level(logging.DEBUG):
        assert await m000_bambuddy_import.seed(None) is None

    assert caplog.records == []
    assert legacy.exists()


def test_m000_still_carries_the_version_0_record():
    """``_bootstrap_existing`` writes version 0 under this exact name on every
    existing install; the module may be inert but it may not lose its identity."""
    assert m000_bambuddy_import.version == 0
    assert m000_bambuddy_import.name == "bambuddy_to_bamdude_301"


# ── The PostgreSQL auto-migrate refuses a foreign file ────────────────────────


async def test_bamdude_db_is_taken_when_present(tmp_path):
    primary = tmp_path / "bamdude.db"
    _sqlite_with(primary, "printers")
    _sqlite_with(tmp_path / "bambuddy.db", "printers")

    assert await _local_sqlite_candidate(tmp_path) == primary


async def test_a_legacy_name_is_taken_only_when_it_is_our_own_301_file(tmp_path):
    legacy = tmp_path / "bambuddy.db"
    _sqlite_with(legacy, "telegram_chats")

    assert await _local_sqlite_candidate(tmp_path) == legacy


async def test_a_foreign_bambuddy_file_is_refused_and_reported(tmp_path, caplog):
    """The regression that mattered: this path *consumes* what it accepts.

    It renames the file to ``.db.migrated`` and unlinks its WAL, so accepting a
    genuine Bambuddy database here would be the removed one-time import wearing
    a different name — and it runs before m000 is ever reached.
    """
    legacy = tmp_path / "bambuddy.db"
    _sqlite_with(legacy, "printers")

    with caplog.at_level(logging.WARNING, logger=PORTABLE_LOGGER):
        assert await _local_sqlite_candidate(tmp_path) is None

    assert _messages(caplog, PORTABLE_LOGGER) == [_expected(legacy)]
    assert legacy.exists()


@pytest.mark.parametrize("name", ["bambuddy.db", "bambutrack.db"])
async def test_both_legacy_names_are_probed(tmp_path, name):
    _sqlite_with(tmp_path / name, "printers")
    assert await _local_sqlite_candidate(tmp_path) is None


async def test_an_empty_data_directory_offers_nothing(tmp_path):
    assert await _local_sqlite_candidate(tmp_path) is None
