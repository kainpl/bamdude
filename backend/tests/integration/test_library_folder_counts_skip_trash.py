"""The folder tree counts the files a folder HAS, not the ones it once had.

⚠️ Reported from a farm: the counts beside each folder in the library tree kept
counting files that had been moved to the trash, so a folder emptied into the
bin still read "12 files" and the number never agreed with what opening it
showed.

The endpoint's own neighbour is the proof it was an oversight rather than a
decision: the *latest activity* query three lines below filters
``deleted_at IS NULL``, and its comment claims to be the "same WHERE clause"
as the count. It was not.

⚠️ Trashed is not deleted. The row stays, the bytes stay, and the file can be
restored — which is exactly why the count has to exclude it explicitly rather
than relying on the row being gone.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from backend.app.models.library import LibraryFile, LibraryFolder

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _folder(db_session, name: str) -> LibraryFolder:
    folder = LibraryFolder(name=name)
    db_session.add(folder)
    await db_session.commit()
    await db_session.refresh(folder)
    return folder


async def _file(db_session, folder: LibraryFolder, name: str, *, trashed: bool = False) -> LibraryFile:
    row = LibraryFile(
        filename=name,
        file_path=f"/tmp/{name}",
        file_size=1,
        file_type="3mf",
        folder_id=folder.id,
        deleted_at=datetime.now(timezone.utc) if trashed else None,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def _count_in_tree(async_client: AsyncClient, folder_id: int) -> int:
    tree = (await async_client.get("/api/v1/library/folders/")).json()

    def walk(nodes):
        for node in nodes:
            if node["id"] == folder_id:
                return node["file_count"]
            found = walk(node.get("children") or [])
            if found is not None:
                return found
        return None

    found = walk(tree)
    assert found is not None, f"folder {folder_id} is not in the tree"
    return found


class TestTheTreeCount:
    async def test_a_trashed_file_stops_being_counted(self, async_client, db_session):
        folder = await _folder(db_session, "counts-trash")
        await _file(db_session, folder, "kept.3mf")
        await _file(db_session, folder, "binned.3mf", trashed=True)

        assert await _count_in_tree(async_client, folder.id) == 1

    async def test_a_folder_emptied_into_the_bin_reads_zero(self, async_client, db_session):
        """The reported shape: the number never reached 0 however much you binned."""
        folder = await _folder(db_session, "counts-all-trashed")
        for name in ("a.3mf", "b.3mf", "c.3mf"):
            await _file(db_session, folder, name, trashed=True)

        assert await _count_in_tree(async_client, folder.id) == 0

    async def test_live_files_are_still_counted(self, async_client, db_session):
        """The guard on the fix: excluding the bin must not exclude everything."""
        folder = await _folder(db_session, "counts-live")
        for name in ("a.3mf", "b.3mf"):
            await _file(db_session, folder, name)

        assert await _count_in_tree(async_client, folder.id) == 2

    def test_every_file_count_decides_about_the_trash_out_loud(self):
        """⚠️ Structural, because the bug was an omission and omissions are
        invisible.

        Three endpoints count a folder's files and two of them had it right,
        which is exactly why nobody noticed the third. So the rule is not "always
        filter" — the delete guard counts trashed files ON PURPOSE, since a
        binned file still occupies the folder it would be deleted from. The rule
        is that the choice is written down: either the filter is there, or a
        comment says why it is not.
        """
        import re
        from pathlib import Path

        source = Path("backend/app/api/routes/library.py").read_text(encoding="utf-8")
        counts = [m.start() for m in re.finditer(r"func\.count\(LibraryFile\.id\)", source)]
        assert counts, "no file count found — has the query moved?"

        undecided = []
        for at in counts:
            window = source[max(0, at - 500) : at + 600]
            # Three ways to be explicit: the clause itself, a filter list whose
            # name says it, or a comment saying the trash is counted on purpose.
            filtered = "LibraryFile.deleted_at.is_(None)" in window or "live_files" in window
            excused = "deleted_at" in window and "deliberately" in window
            if not (filtered or excused):
                undecided.append(source[:at].count(chr(10)) + 1)

        assert not undecided, f"library.py:{undecided} counts LibraryFile without saying whether the trash is in or out"
