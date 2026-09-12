"""One resolver for "file name on the printer's card → archive" (spec §3.1).

The printer-card cover used to carry this query inline; the file manager
needs the same answer, so it moved to services/archive.py and both call it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.services.archive import find_archive_for_sd_file, sd_stem


def _archive(
    printer_id: int,
    filename: str,
    *,
    file_path: str,
    thumbnail_path: str | None = None,
    print_name: str | None = None,
    minutes_ago: int = 0,
):
    return PrintArchive(
        printer_id=printer_id,
        filename=filename,
        file_path=file_path,
        thumbnail_path=thumbnail_path,
        file_size=7,
        print_name=print_name or sd_stem(filename),
        source_content_hash="a" * 64,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


async def _printer(db, serial: str) -> Printer:
    p = Printer(name=f"p-{serial}", ip_address="10.0.0.1", access_code="00000000", serial_number=serial, model="X1C")
    db.add(p)
    await db.flush()
    return p


def _on_disk(tmp_path, monkeypatch, rel: str) -> str:
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"PK")  # existence is all the resolver checks
    return rel


class TestStem:
    def test_strips_repeated_suffixes(self):
        assert sd_stem("part.gcode.3mf") == "part"
        assert sd_stem("part.3mf") == "part"
        assert sd_stem("part.gcode.3mf.3mf") == "part"
        assert sd_stem("part") == "part"

    def test_a_non_string_is_refused_before_the_loop(self):
        """A duck-typed ``endswith`` never escapes the loop — derive_remote_filename
        carries the same guard because that allocation OOM'd the test runner."""
        with pytest.raises(TypeError):
            sd_stem(None)


@pytest.mark.asyncio
async def test_dispatched_name_with_spaces_matches_the_underscored_card_name(db_session, tmp_path, monkeypatch):
    p = await _printer(db_session, "S1")
    rel = _on_disk(tmp_path, monkeypatch, "archive/1/My Model.gcode.3mf")
    db_session.add(_archive(p.id, "My Model.gcode.3mf", file_path=rel))
    await db_session.commit()
    found = await find_archive_for_sd_file(db_session, p.id, "My_Model.3mf")
    assert found is not None and found.filename == "My Model.gcode.3mf"


@pytest.mark.asyncio
async def test_external_name_matches_itself(db_session, tmp_path, monkeypatch):
    p = await _printer(db_session, "S2")
    rel = _on_disk(tmp_path, monkeypatch, "archive/1/part.gcode.3mf")
    db_session.add(_archive(p.id, "part.gcode.3mf", file_path=rel))
    await db_session.commit()
    assert (await find_archive_for_sd_file(db_session, p.id, "part.gcode.3mf")) is not None
    assert (await find_archive_for_sd_file(db_session, p.id, "part.3mf")) is not None


@pytest.mark.asyncio
async def test_newest_populated_row_wins_and_a_provisional_row_never_shadows(db_session, tmp_path, monkeypatch):
    p = await _printer(db_session, "S3")
    old = _on_disk(tmp_path, monkeypatch, "archive/1/old/part.3mf")
    new = tmp_path / "archive/1/new/part.3mf"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"PK")
    db_session.add(_archive(p.id, "part.3mf", file_path=old, minutes_ago=60))
    db_session.add(_archive(p.id, "part.3mf", file_path="archive/1/new/part.3mf", minutes_ago=1))
    db_session.add(_archive(p.id, "part.3mf", file_path="", minutes_ago=0))  # 3MF still downloading
    await db_session.commit()
    found = await find_archive_for_sd_file(db_session, p.id, "part.3mf")
    assert found is not None and found.file_path == "archive/1/new/part.3mf"


@pytest.mark.asyncio
async def test_a_row_whose_file_is_gone_yields_to_the_next(db_session, tmp_path, monkeypatch):
    p = await _printer(db_session, "S4")
    older = _on_disk(tmp_path, monkeypatch, "archive/1/older/part.3mf")
    db_session.add(_archive(p.id, "part.3mf", file_path=older, minutes_ago=60))
    db_session.add(_archive(p.id, "part.3mf", file_path="archive/1/vanished/part.3mf", minutes_ago=1))
    await db_session.commit()
    found = await find_archive_for_sd_file(db_session, p.id, "part.3mf")
    assert found is not None and found.file_path == older


@pytest.mark.asyncio
async def test_a_retention_cleaned_row_answers_with_its_surviving_png(db_session, tmp_path, monkeypatch):
    """Retention deletes the 3MF and blanks ``file_path`` but keeps the PNG,
    because the row is print history — so the picture is still an answer."""
    p = await _printer(db_session, "S7")
    png = _on_disk(tmp_path, monkeypatch, "archive/1/cleaned/part.png")
    db_session.add(_archive(p.id, "part.3mf", file_path="", thumbnail_path=png))
    await db_session.commit()
    found = await find_archive_for_sd_file(db_session, p.id, "part.3mf")
    assert found is not None and found.thumbnail_path == png


@pytest.mark.asyncio
async def test_a_pending_row_with_nothing_on_disk_yet_is_not_an_answer(db_session, tmp_path, monkeypatch):
    """The other kind of empty ``file_path``: created at print start, 3MF still
    being fetched, no thumbnail either. Nothing to show, so it is not the answer."""
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    p = await _printer(db_session, "S8")
    db_session.add(_archive(p.id, "part.3mf", file_path=""))
    await db_session.commit()
    assert (await find_archive_for_sd_file(db_session, p.id, "part.3mf")) is None


@pytest.mark.asyncio
async def test_a_cleaned_newer_row_beats_a_populated_older_one(db_session, tmp_path, monkeypatch):
    """Newest still wins: the cover shows the last print's picture, not an older
    print's 3MF that happens to have survived retention."""
    p = await _printer(db_session, "S9")
    old_3mf = _on_disk(tmp_path, monkeypatch, "archive/1/older/part.3mf")
    new_png = tmp_path / "archive/1/newer/part.png"
    new_png.parent.mkdir(parents=True)
    new_png.write_bytes(b"\x89PNG")
    db_session.add(_archive(p.id, "part.3mf", file_path=old_3mf, minutes_ago=60))
    db_session.add(_archive(p.id, "part.3mf", file_path="", thumbnail_path="archive/1/newer/part.png", minutes_ago=1))
    await db_session.commit()
    found = await find_archive_for_sd_file(db_session, p.id, "part.3mf")
    assert found is not None and found.thumbnail_path == "archive/1/newer/part.png"


@pytest.mark.asyncio
async def test_the_print_name_arm_matches_what_no_filename_arm_can(db_session, tmp_path, monkeypatch):
    """A dispatched file renamed on the card: only ``print_name == stem`` can see it."""
    p = await _printer(db_session, "S10")
    rel = _on_disk(tmp_path, monkeypatch, "archive/1/unrelated.gcode.3mf")
    db_session.add(_archive(p.id, "unrelated.gcode.3mf", file_path=rel, print_name="part"))
    await db_session.commit()
    found = await find_archive_for_sd_file(db_session, p.id, "part.3mf")
    assert found is not None and found.print_name == "part"


@pytest.mark.asyncio
async def test_another_printers_row_and_no_row_answer_none(db_session, tmp_path, monkeypatch):
    p1 = await _printer(db_session, "S5")
    p2 = await _printer(db_session, "S6")
    rel = _on_disk(tmp_path, monkeypatch, "archive/1/part.3mf")
    db_session.add(_archive(p1.id, "part.3mf", file_path=rel))
    await db_session.commit()
    assert (await find_archive_for_sd_file(db_session, p2.id, "part.3mf")) is None
    assert (await find_archive_for_sd_file(db_session, p1.id, "other.3mf")) is None
