"""A Bambuddy database in the data directory survives the boot, untouched.

Until 0.5.6 ``run_all_migrations`` ended by renaming the legacy database to
``.db.bak`` and **unlinking** its ``-wal`` / ``-shm`` sidecars. That existed for
one reason, stated in its own comment: "prevent re-import on next start". With
the importer removed on 2026-09-05 the reason is gone, and what was left behind
was actively harmful — nothing reads the file any more, yet the boot would still
rename a database we no longer understand and throw away its un-checkpointed
transactions.

It is also what makes the message m000 logs true: it tells the operator the file
is *left untouched*, and that the file is theirs to remove when they are done
with it. A log line contradicted by the code twenty lines away is worse than no
log line, so the promise is pinned here rather than trusted.

This drives the real ``run_all_migrations`` because the deleted code sat after
``_run_pending`` — there is no smaller seam that could observe it.
"""

import sqlite3

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import import_all_models
from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations import run_all_migrations


def _make_bambuddy_db(path) -> None:
    """A plausible upstream file: real SQLite, and no ``telegram_chats``.

    The missing table is what tells ``_is_bamdude_301`` this is genuinely
    upstream's rather than our own 3.0.1-era ``bambuddy.db``.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO printers (name) VALUES ('upstream P1S')")
        conn.commit()
    finally:
        conn.close()


async def test_a_bambuddy_database_and_its_wal_survive_a_full_boot(tmp_path, monkeypatch):
    db_path = tmp_path / "bamdude.db"
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    assert is_sqlite(), "the legacy-file handling only runs on SQLite - this test would pass vacuously"

    legacy = tmp_path / "bambuddy.db"
    _make_bambuddy_db(legacy)
    wal = tmp_path / "bambuddy.db-wal"
    wal.write_bytes(b"")  # empty: SQLite ignores it, and it is here to be counted afterwards

    import_all_models()  # what init_db() does before the chain; create_all needs a full metadata
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await run_all_migrations(engine, session_factory)
    finally:
        await engine.dispose()

    assert legacy.exists(), "the boot renamed the Bambuddy database - m000 promises it is left untouched"
    assert wal.exists(), "the boot deleted the Bambuddy database's WAL - un-checkpointed data would be lost"
    assert not (tmp_path / "bambuddy.db.bak").exists(), "the .bak rename is back"

    with sqlite3.connect(str(legacy)) as conn:
        assert conn.execute("SELECT name FROM printers").fetchall() == [("upstream P1S",)], (
            "the Bambuddy database was opened for writing"
        )

    assert db_path.exists(), "BamDude's own database was not created alongside the legacy file"
