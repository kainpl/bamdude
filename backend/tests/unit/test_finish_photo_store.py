"""Where a finish photo is written, and what becomes of one that is refused (G10 G, M30)."""

from __future__ import annotations

import ast
import inspect
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
    row = SimpleNamespace(id=7, recovery_identity="run-a", file_path="archive/3/print-a/a.gcode.3mf")
    archive = {"row": row}
    return SimpleNamespace(root=tmp_path, archive=archive, row=row, session=_sessions(archive))


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
