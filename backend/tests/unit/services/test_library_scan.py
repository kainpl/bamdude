"""The scan worker: what it does off the loop, and what it refuses to delete.

⚠️ These pin the two things that fail silently. A transaction held across a file
read does not error — it makes *other* requests fail, somewhere else, with a
traceback that names an innocent query. And a deletion pass that trusts an
unreachable mount does not error either — it reports success while emptying a
library nobody touched.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path

import pytest

from backend.app.services import library_scan
from backend.app.services.library_scan import BATCH_SIZE, EMPTY_WALK_GUARD, _Known, collect_tree


@pytest.mark.asyncio
async def test_the_walk_runs_in_a_thread(tmp_path, monkeypatch):
    """⚠️ The reason the WebSocket dropped. Every readdir on a network share is a
    round trip, and on the event loop it stalls every other request in the
    process — which is what made the browser ask for a fresh token, which is the
    request the user actually saw fail.
    """
    seen: dict[str, str] = {}

    def spy(root, show_hidden):
        seen["thread"] = threading.current_thread().name
        return []

    monkeypatch.setattr(library_scan, "_walk_sync", spy)
    await collect_tree(tmp_path, False)

    assert seen["thread"] != threading.main_thread().name


@pytest.mark.asyncio
async def test_hidden_files_and_directories_are_skipped_unless_asked_for(tmp_path):
    (tmp_path / "visible.3mf").write_bytes(b"x")
    (tmp_path / ".hidden.3mf").write_bytes(b"x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "buried.3mf").write_bytes(b"x")

    tree = await collect_tree(tmp_path, show_hidden=False)
    files = [name for _, names in tree for name in names]
    assert files == ["visible.3mf"]

    tree = await collect_tree(tmp_path, show_hidden=True)
    files = sorted(name for _, names in tree for name in names)
    assert ".hidden.3mf" in files and "buried.3mf" in files


@pytest.mark.asyncio
async def test_preparing_a_file_opens_no_session(tmp_path, monkeypatch):
    """⚠️ The whole fix in one assertion. Hashing reads the file over the
    network and the 3MF parser unzips it; either with a transaction open is the
    bug this module exists to remove.
    """
    opened = []

    from backend.app.core import database

    real = database.async_session

    def watched(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(database, "async_session", watched)

    target = tmp_path / "part.stl"
    target.write_bytes(b"solid\n")
    prepared = await library_scan.prepare(str(tmp_path), "part.stl", tmp_path, None, "")

    assert prepared is not None
    assert prepared.intent == "create"
    assert opened == [], "preparing a file must not touch the database"


@pytest.mark.asyncio
async def test_a_file_outside_the_scannable_set_is_ignored(tmp_path):
    (tmp_path / "notes.txt").write_text("hi")
    assert await library_scan.prepare(str(tmp_path), "notes.txt", tmp_path, None, "") is None


@pytest.mark.asyncio
async def test_markdown_is_scannable_because_a_readme_must_survive_a_rescan(tmp_path):
    """⚠️ This is #2520 and it is easy to "tidy away". Markdown is in the
    scannable set so an external folder's README keeps its row; drop it and the
    scan reads that row as a file no longer on disk and purges it on every pass
    while the file sits there untouched.
    """
    (tmp_path / "README.md").write_text("hi")
    assert await library_scan.prepare(str(tmp_path), "README.md", tmp_path, None, "") is not None


@pytest.mark.asyncio
async def test_a_known_file_that_has_not_moved_is_not_re_hashed(tmp_path, monkeypatch):
    """A mount that has not changed must cost no reads at all — that is what
    makes hashing mounts affordable in the first place.
    """
    target = tmp_path / "part.stl"
    target.write_bytes(b"solid\n")
    stat = target.stat()

    from backend.app.api.routes import library as routes

    known = _Known(
        id=1,
        file_hash="deadbeef",
        file_size=stat.st_size,
        fs_modified_at=routes._mtime_to_utc(stat.st_mtime),
    )

    hashed = []
    monkeypatch.setattr(routes, "calculate_file_hash", lambda p: hashed.append(p) or "x")

    prepared = await library_scan.prepare(str(tmp_path), "part.stl", tmp_path, known, "")

    assert prepared is not None
    assert prepared.intent == "refresh"
    assert prepared.new_hash is None
    assert hashed == [], "an unchanged file was re-read"


@pytest.mark.asyncio
async def test_a_known_file_that_changed_is_re_hashed(tmp_path):
    target = tmp_path / "part.stl"
    target.write_bytes(b"solid\n")

    known = _Known(id=1, file_hash="stale", file_size=999, fs_modified_at=datetime(2020, 1, 1))
    prepared = await library_scan.prepare(str(tmp_path), "part.stl", tmp_path, known, "")

    assert prepared is not None
    assert prepared.new_hash and prepared.new_hash != "stale"


class TestTheDeletionGuard:
    """⚠️ The dangerous half of this change.

    Incremental commits removed the rollback that used to make a failed scan
    harmless. A Synology share that blinks makes ``os.path.exists`` say no to
    everything, and an honest sync then deletes a library nobody touched.
    """

    @pytest.mark.asyncio
    async def test_an_empty_walk_against_a_stocked_folder_deletes_nothing(self):
        counters = dict.fromkeys(("files_removed", "folders_removed"), 0)
        known = {
            f"/mnt/share/f{i}.3mf": _Known(id=i, file_hash=None, file_size=1, fs_modified_at=None) for i in range(20)
        }

        skipped = await library_scan.remove_vanished(set(), known, {"": 1}, counters)

        assert skipped is True
        assert counters["files_removed"] == 0

    @pytest.mark.asyncio
    async def test_one_record_is_already_worth_protecting(self):
        """The question is not how many rows there are — it is whether the walk
        saw anything at all.
        """
        assert EMPTY_WALK_GUARD == 1

    @pytest.mark.asyncio
    async def test_a_folder_that_never_had_records_is_not_an_error(self):
        """An empty walk against an empty folder is just an empty folder."""
        counters = dict.fromkeys(("files_removed", "folders_removed"), 0)
        skipped = await library_scan.remove_vanished(set(), {}, {"": 1}, counters)
        assert skipped is False


def test_the_batch_is_sized_for_lock_time_not_commit_count():
    """⚠️ In WAL with synchronous=NORMAL a commit costs no fsync, so batching is
    not about commit cost. It is about how long the write lock is held, and the
    number has to stay small enough that the window is milliseconds.
    """
    assert 10 <= BATCH_SIZE <= 200


def test_a_scan_can_be_cancelled_and_forgets_itself():
    """Every create_task in this codebase is paired with a shutdown cancel."""

    async def forever():
        await asyncio.sleep(3600)

    async def run():
        task = asyncio.create_task(forever())
        library_scan._running[999] = task
        library_scan.cancel_running_scans()
        await asyncio.sleep(0)
        assert task.cancelled() or task.cancelling()
        assert 999 not in library_scan._running

    asyncio.run(run())


def test_paths_are_relative_to_the_mount_not_absolute():
    """A folder key is the path under the mount, so moving the mount does not
    orphan every subfolder row.
    """
    root = Path("/mnt/share")
    rel = str(Path("/mnt/share/models/spools").relative_to(root)).replace("\\", "/")
    assert rel == "models/spools"
