"""One archive belongs to at most one folder — DB-level, no longer API-reachable.

``library_folders.archive_id`` was a plain nullable FK, so any number of folders
could claim one archive — while the archives page only ever drew the first it
found (``linkedFolders[0]``). A second binding existed in the database and
nowhere on screen.

The unique index below still guards the column at the DB level (m133), but the
folder-to-archive LINK itself was later cut entirely from the API (archives are
print history, not a filing destination — folder-to-project is the surviving
link). ``LibraryFolder.archive_id`` stays dormant: the column and its index are
untouched (migrations are frozen), and no route can ever set it again, so the
route-level "refuse rather than steal" write-path gate this file used to test
was deleted along with the endpoints that called it.

⚠️ **The unique index is what makes it true** for whatever legacy data still
carries a value. Without the index a future writer that forgets to ask would put
the database back into the state the archives page cannot show.

⚠️ **NULLs are exempt.** Unlinked folders are the ordinary case, and both SQLite
and PostgreSQL allow any number of NULLs in a unique index — which is the whole
reason this is expressible as one.
"""

from __future__ import annotations

import inspect

from backend.app.migrations import m133_one_folder_per_archive as m133
from backend.app.models.library import LibraryFolder

_INDEX = "ix_library_folders_archive_id_unique"


class TestTheIndex:
    def test_the_model_declares_it_for_fresh_installs(self) -> None:
        """Two places, as every schema change here needs: the model for
        ``create_all`` and the migration for databases that already exist."""
        indexes = {i.name: i for i in LibraryFolder.__table__.indexes}

        assert _INDEX in indexes
        assert indexes[_INDEX].unique is True
        assert [c.name for c in indexes[_INDEX].columns] == ["archive_id"]

    def test_the_migration_creates_the_same_name(self) -> None:
        """A different name on the two paths would leave upgraded and fresh
        installs holding differently-named copies of one rule."""
        assert _INDEX in inspect.getsource(m133)

    def test_it_is_an_index_not_a_table_constraint(self) -> None:
        """⚠️ Adding a table constraint to SQLite means recreating the table; an
        index is one statement on both back ends."""
        src = inspect.getsource(m133)

        assert "CREATE UNIQUE INDEX" in src
        assert "ALTER TABLE" not in src


class TestTheMigrationResolvesDuplicatesFirst:
    def test_it_clears_losers_before_creating_the_index(self) -> None:
        """The index cannot be created while duplicates exist, so the order is
        not a preference."""
        src = inspect.getsource(m133)

        assert src.index("UPDATE library_folders SET archive_id = NULL") < src.index("CREATE UNIQUE INDEX")

    def test_the_newest_binding_wins(self) -> None:
        """Later is the more deliberate one; an older link is likelier to be the
        forgotten half of the pair."""
        assert "ORDER BY updated_at DESC, id DESC" in inspect.getsource(m133)

    def test_every_clearing_is_logged_with_both_names(self) -> None:
        """⚠️ This is the one moment an existing link disappears without anybody
        pressing anything, so it must not be silent."""
        src = inspect.getsource(m133)

        assert "logger.warning" in src
        assert "clearing" in src
