"""m000 imports nothing since 0.5.6 — it only names a Bambuddy file in the log.

The import of an upstream Bambuddy 2.2.2 database was removed on 2026-09-05.
What is left has to get two things right, and both are silent when broken: it
must speak up for a *genuine* Bambuddy file (or an operator who dropped one in
expecting an import is left with no explanation for why nothing happened), and
it must stay quiet for our OWN 3.0.1-era ``bambuddy.db``, which ``__init__.py``
has already renamed to ``bamdude.db`` and upgraded before this ever runs.

The file surviving the boot untouched is the other half of the promise and
cannot be seen from here — it is pinned by
``integration/test_legacy_bambuddy_file_survives_boot.py``.
"""

import logging
import sqlite3

import pytest

from backend.app.core.config import settings
from backend.app.migrations import m000_bambuddy_import

LOGGER_NAME = "backend.app.migrations.m000_bambuddy_import"


def _make_legacy_db(path, table: str) -> None:
    """A real SQLite file, so ``_is_bamdude_301`` reads it rather than erroring out."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


async def test_a_genuine_bambuddy_file_is_named_in_the_log_and_left_alone(data_dir, caplog):
    """The whole remaining purpose of the module."""
    legacy = data_dir / "bambuddy.db"
    _make_legacy_db(legacy, "printers")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        await m000_bambuddy_import.seed(None)

    warnings = [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {[r.getMessage() for r in warnings]}"

    message = warnings[0].getMessage()
    assert str(legacy) in message, "the operator cannot act on a warning that does not name the file"
    assert "removed in 0.5.6" in message
    assert "https://docs.bamdude.top/getting-started/upgrading/" in message

    assert legacy.exists(), "the stub must not consume the file it is only reporting"


async def test_nothing_is_logged_when_no_legacy_database_is_present(data_dir, caplog):
    """The overwhelmingly common case: a normal install must boot in silence."""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        await m000_bambuddy_import.seed(None)

    assert [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME] == []


async def test_our_own_301_database_is_not_reported_as_bambuddy(data_dir, caplog, monkeypatch):
    """A ``bambuddy.db`` written by BamDude 3.0.1 is ours, and already handled.

    ``__init__.py`` renames it to ``bamdude.db`` before the chain runs, so by the
    time m000 is reached there is normally nothing left to find. Warning about it
    would tell a BamDude user upgrading from 3.0.1 that their own database was
    ignored, which is both false and alarming.
    """
    legacy = data_dir / "bambuddy.db"
    _make_legacy_db(legacy, "telegram_chats")

    seen = []

    async def fake_is_bamdude_301(path):
        seen.append(path)
        return True

    monkeypatch.setattr("backend.app.migrations._is_bamdude_301", fake_is_bamdude_301)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        await m000_bambuddy_import.seed(None)

    assert seen == [legacy], "the stub must ask whether the file is ours before reporting it"
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME] == []


async def test_the_real_telegram_chats_probe_agrees(data_dir, caplog):
    """The same case again without the patch, so the two halves cannot drift.

    ``_is_bamdude_301`` decides on the presence of ``telegram_chats``. If that
    ever changes, the patched test above would keep passing against a stub while
    the real boot started warning about every 3.0.1 database.
    """
    _make_legacy_db(data_dir / "bambuddy.db", "telegram_chats")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        await m000_bambuddy_import.seed(None)

    assert [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME] == []
