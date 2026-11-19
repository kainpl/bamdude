"""Where a finish photo is written, and what becomes of one that is refused (G10 G, M30)."""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import finish_photo_store as store

APP = Path(__file__).resolve().parents[2] / "app"


def _sessions(archive):
    """A session factory whose ``get`` answers ``archive`` as it is at that moment."""

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=AsyncMock(side_effect=lambda _model, _id: archive["row"]))

    return session


@pytest.fixture
def box(tmp_path, monkeypatch):
    from backend.app.utils import archive_paths

    monkeypatch.setattr(archive_paths.settings, "base_dir", tmp_path)
    monkeypatch.setattr(archive_paths.settings, "archive_dir", tmp_path / "archive")
    row = SimpleNamespace(id=7, recovery_identity="run-a", file_path="archive/3/print-a/a.gcode.3mf", photos=None)
    archive = {"row": row}
    store._own.clear()
    yield SimpleNamespace(root=tmp_path, archive=archive, row=row, session=_sessions(archive))
    store._own.clear()


@pytest.mark.asyncio
async def test_a_frame_is_written_into_the_archives_own_folder(box):
    name = await store.save_frame(7, "run-a", b"jpeg", session=box.session)
    assert name and name.startswith("finish_") and name.endswith(".jpg")
    assert (box.root / "archive" / "3" / "print-a" / "photos" / name).read_bytes() == b"jpeg"


@pytest.mark.asyncio
async def test_the_folder_is_the_one_the_archive_has_at_the_write(box):
    box.row.file_path = ""  # no 3MF (yet): the folder the id owns
    first = await store.save_frame(7, "run-a", b"1", session=box.session)
    assert (box.root / "archive" / "no_source" / "7" / "photos" / first).is_file()
    box.row.file_path = "archive/3/print-a/a.gcode.3mf"  # the 3MF arrived
    second = await store.save_frame(7, "run-a", b"2", session=box.session)
    assert (box.root / "archive" / "3" / "print-a" / "photos" / second).is_file()


@pytest.mark.asyncio
async def test_an_id_that_now_belongs_to_another_archive_gets_nothing(box):
    """SQLite hands a deleted archive's id to the next one: the same number, another print."""
    box.archive["row"] = SimpleNamespace(id=7, recovery_identity="run-b", file_path="archive/3/print-b/b.gcode.3mf")
    assert await store.save_frame(7, "run-a", b"jpeg", session=box.session) is None
    assert not (box.root / "archive" / "3" / "print-b" / "photos").exists()
    box.archive["row"] = None
    assert await store.save_frame(7, "run-a", b"jpeg", session=box.session) is None


@pytest.mark.asyncio
async def test_a_terminal_that_no_longer_owns_the_printer_writes_nothing(box):
    assert await store.save_frame(7, "run-a", b"jpeg", session=box.session, may_run=lambda: False) is None
    photos = box.root / "archive" / "3" / "print-a" / "photos"
    assert not photos.exists() or list(photos.iterdir()) == []


@pytest.mark.asyncio
async def test_a_refused_photo_is_removed_wherever_it_was_written(box):
    box.row.file_path = ""
    name = await store.save_frame(7, "run-a", b"jpeg", session=box.session)
    written = box.root / "archive" / "no_source" / "7" / "photos" / name
    box.row.file_path = "archive/3/print-a/a.gcode.3mf"  # the folder moved before the refusal
    await store.discard(7, name, session=box.session)
    assert not written.exists()


@pytest.mark.asyncio
async def test_discarding_what_is_not_there_is_quiet(box):
    await store.discard(7, "finish_20260101_000000_deadbeef.jpg", session=box.session)
    await store.discard(7, None, session=box.session)
    box.archive["row"] = None
    await store.discard(7, "finish_20260101_000000_deadbeef.jpg", session=box.session)


@pytest.mark.asyncio
async def test_a_refused_photo_is_removed_where_it_was_written_even_after_the_id_was_reused(box):
    """Codex G2: looked for through the NEW row of a reused id, the old print's own file was never found."""
    folder = await store.photos_dir_at_write(7, "run-a", session=box.session)
    name = await store.save_frame(7, "run-a", b"jpeg", session=box.session)
    box.archive["row"] = SimpleNamespace(id=7, recovery_identity="run-b", file_path="archive/3/print-b/b.gcode.3mf")
    await store.discard(7, name, session=box.session)
    assert not (folder / name).exists()
    assert name not in store._own


@pytest.mark.asyncio
async def test_a_confirmed_photo_is_no_longer_the_stores_to_remove(box):
    name = await store.save_frame(7, "run-a", b"jpeg", session=box.session)
    assert name in store._own
    store.confirm(name)
    assert name not in store._own
    box.row.photos = [name]
    await store.discard(7, name, session=box.session)  # a later mistake finds it only through the row
    assert not (box.root / "archive" / "3" / "print-a" / "photos" / name).exists()


@pytest.mark.asyncio
async def test_a_frame_that_could_not_be_written_leaves_nothing(box, monkeypatch):
    def full(self, _data):
        self.touch()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", full)
    with pytest.raises(OSError):
        await store.save_frame(7, "run-a", b"jpeg", session=box.session)
    photos = box.root / "archive" / "3" / "print-a" / "photos"
    assert list(photos.iterdir()) == [] and store._own == {}


class TestAnUncertainCommit:
    """Codex G2: a commit that failed after the frame was written left the JPG on disk."""

    @pytest.mark.asyncio
    async def test_a_photo_the_archive_does_not_list_is_removed(self, box):
        box.row.photos = None
        name = await store.save_frame(7, "run-a", b"jpeg", session=box.session)
        path = store._own[name]
        assert await store.settle_uncertain(7, "run-a", name, session=box.session) is False
        assert not path.exists() and name not in store._own

    @pytest.mark.asyncio
    async def test_a_photo_the_archive_lists_after_all_is_kept(self, box):
        """The commit took and its answer was lost: the file is the archive's now."""
        name = await store.save_frame(7, "run-a", b"jpeg", session=box.session)
        path = store._own[name]
        box.row.photos = ["older.jpg", name]
        assert await store.settle_uncertain(7, "run-a", name, session=box.session) is True
        assert path.is_file() and name not in store._own

    @pytest.mark.asyncio
    async def test_an_archive_that_became_another_does_not_keep_it(self, box):
        name = await store.save_frame(7, "run-a", b"jpeg", session=box.session)
        path = store._own[name]
        box.archive["row"] = SimpleNamespace(id=7, recovery_identity="run-b", file_path="", photos=[name])
        assert await store.settle_uncertain(7, "run-a", name, session=box.session) is False
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_when_the_archive_cannot_be_read_nothing_is_removed_on_a_guess(self, box):
        name = await store.save_frame(7, "run-a", b"jpeg", session=box.session)
        path = store._own[name]

        @asynccontextmanager
        async def broken():
            raise RuntimeError("the database went away")
            yield  # pragma: no cover

        assert await store.settle_uncertain(7, "run-a", name, session=broken) is None
        assert path.is_file() and name in store._own
        store.confirm(name)


class TestTheBuiltInCamera:
    """Codex G1: the folder was chosen before the capture; a 3MF attached during it moved the archive."""

    @pytest.mark.asyncio
    async def test_a_frame_is_captured_first_and_written_where_the_archive_is_then(self, box, monkeypatch):
        from backend.app.services import camera, camera_runtime

        box.row.file_path = ""  # no 3MF yet: the folder the id owns
        requests = []

        async def capture(request):
            requests.append(request)
            box.row.file_path = "archive/3/print-a/a.gcode.3mf"  # the 3MF arrives during the capture
            return SimpleNamespace(frame=b"captured")

        monkeypatch.setattr(camera_runtime, "capture", capture)
        frame = await camera.capture_finish_frame(printer_id=1, ip_address="127.0.0.1", access_code="", model=None)
        assert frame == b"captured" and len(requests) == 1
        name = await store.save_frame(7, "run-a", frame, session=box.session)
        assert (box.root / "archive" / "3" / "print-a" / "photos" / name).read_bytes() == b"captured"
        old = box.root / "archive" / "no_source" / "7" / "photos"
        assert not old.exists() or list(old.iterdir()) == []
        store.confirm(name)

    @pytest.mark.asyncio
    async def test_a_camera_that_gives_nothing_or_fails_gives_no_frame(self, monkeypatch):
        from backend.app.services import camera, camera_runtime

        async def nothing(_request):
            return SimpleNamespace(frame=None)

        async def broken(_request):
            raise RuntimeError("the camera worker is gone")

        for capture in (nothing, broken):
            monkeypatch.setattr(camera_runtime, "capture", capture)
            assert await camera.capture_finish_frame(1, "127.0.0.1", "", None) is None

    @pytest.mark.asyncio
    async def test_the_frame_is_rotated_like_every_other_source(self, monkeypatch):
        from backend.app.services import camera, camera_runtime

        async def capture(_request):
            return SimpleNamespace(frame=b"upright")

        monkeypatch.setattr(camera_runtime, "capture", capture)
        monkeypatch.setattr(camera, "apply_camera_rotation", lambda data, rotation, _logger: data + bytes([rotation]))
        assert await camera.capture_finish_frame(1, "127.0.0.1", "", None, rotation=90) == b"upright" + bytes([90])

    def test_the_built_in_capture_no_longer_takes_a_folder(self):
        """There is no way left to hand it a folder chosen before the capture."""
        from backend.app.services import camera

        assert not hasattr(camera, "capture_finish_photo")
        assert "archive_dir" not in inspect.signature(camera.capture_finish_frame).parameters


def test_two_names_never_collide():
    assert len({store.new_name() for _ in range(200)}) == 200


# -- no motion for the sake of a photo (owner decision 2026-09-14) ----------------------

_COMMAND_WORDS = (
    "send_gcode",
    "gcode_line",
    "execute_macro",
    "publish(",
    "print_resume",
    "home_axes",
    "move_axis",
    "bed_jog",
    "set_timelapse",
    "ipcam_record_set",
    "G90",
    "G91",
    "G1 ",
    "G28",
    "M400",
)


def _function_sources(path: Path, names: set[str]) -> dict[str, str]:
    source = path.read_text(encoding="utf-8")
    found = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            found[node.name] = ast.get_source_segment(source, node) or ""
    return found


def test_no_finish_photo_code_sends_a_printer_command():
    """M30: a finish photo is taken from what the cameras already give — never by moving the printer,
    switching its recording on or sending it G-code. The trigger policy is not this unit's to change."""
    names = {"_capture_finish_photo_from_timelapse", "_background_finish_photo", "on_finish_photo_moment"}
    sources = _function_sources(APP / "main.py", names)
    assert set(sources) == names, sorted(names - set(sources))
    sources["finish_photo_store"] = inspect.getsource(store)
    offenders = [(name, word) for name, text in sources.items() for word in _COMMAND_WORDS if word in text]
    assert offenders == []


class TestACancelDoesNotCutTheWorkInHalf:
    """Codex G2-R: a cancel during the write removed the file before the thread had created it."""

    @pytest.mark.asyncio
    async def test_a_cancel_during_the_write_waits_for_it_and_leaves_nothing(self, box, monkeypatch):
        entered, release = threading.Event(), threading.Event()
        real = Path.write_bytes

        def slow(path, data):
            entered.set()
            assert release.wait(10)
            return real(path, data)

        monkeypatch.setattr(Path, "write_bytes", slow)
        saving = asyncio.create_task(store.save_frame(7, "run-a", b"jpeg", session=box.session))
        assert await asyncio.to_thread(entered.wait, 3)
        saving.cancel()
        await asyncio.sleep(0.1)
        assert not saving.done()  # it waits for the thread before it removes anything
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await saving
        photos = box.root / "archive" / "3" / "print-a" / "photos"
        assert list(photos.iterdir()) == [] and store._own == {}

    @pytest.mark.asyncio
    async def test_the_work_ends_first_and_the_cancel_is_passed_on(self):
        finished = asyncio.Event()

        async def work():
            await asyncio.sleep(0.1)
            finished.set()
            return "result"

        caller = asyncio.create_task(store.to_the_end(work()))
        await asyncio.sleep(0.02)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert finished.is_set()
        assert await store.to_the_end(work()) == "result"  # uncancelled: just the result
