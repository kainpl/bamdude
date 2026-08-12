"""One archive belongs to at most one folder.

``library_folders.archive_id`` was a plain nullable FK, so any number of folders
could claim one archive — while the archives page only ever drew the first it
found (``linkedFolders[0]``). A second binding existed in the database and
nowhere on screen.

⚠️ **The route refuses rather than steals.** Re-pointing an archive silently
would unlink whichever folder held it — a folder the person doing this is not
looking at and may not know exists. Naming the holder costs one step and
destroys nothing.

⚠️ **The unique index is what makes it true**; the 409 is what makes it
explainable. Without the index a future writer that forgets to ask would put the
database back into the state the archives page cannot show. Without the 409, the
same writer's user meets an integrity error.

⚠️ **NULLs are exempt.** Unlinked folders are the ordinary case, and both SQLite
and PostgreSQL allow any number of NULLs in a unique index — which is the whole
reason this is expressible as one.
"""

from __future__ import annotations

import inspect

from backend.app.api.routes import library as library_routes
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


class TestTheRouteGate:
    def _source(self) -> str:
        src = inspect.getsource(library_routes._assert_archive_unclaimed)
        return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    def test_it_refuses_with_409_and_names_the_holder(self) -> None:
        src = self._source()

        assert "409" in src
        assert "holder.name" in src

    def test_it_excludes_the_folder_being_edited(self) -> None:
        """⚠️ Otherwise re-saving a folder that already holds the archive would
        refuse on the strength of its own binding."""
        assert "LibraryFolder.id != folder_id" in self._source()

    def test_both_write_paths_ask(self) -> None:
        """Creating a folder with an archive and re-pointing an existing one are
        two doors into the same table."""
        for fn in (library_routes.create_folder, library_routes.update_folder):
            assert "_assert_archive_unclaimed" in inspect.getsource(fn)
