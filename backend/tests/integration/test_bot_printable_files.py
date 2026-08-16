""" "Print from Library" in the bot must list the files that can be printed.

⚠️ It listed the opposite. `_get_library_files` selected
``file_type == "3mf"`` — but ``detect_file_type`` collapses ``.gcode.3mf`` to
``"gcode"`` and leaves ``"3mf"`` for **unsliced project packages**. Measured
against a live farm on 2026-08-16: 103 sliced files hidden, 29 unprintable
models offered instead, with names like "Bambu Lab P2S Hot Air Deflector
ASA.3mf".

A user picking one of those got as far as the printer, which answered thirty
seconds later that it could not parse the file.
"""

from __future__ import annotations

import pytest

from backend.app.models.library import LibraryFile
from backend.app.services.library_helpers import SLICED_GCODE_META_KEY
from backend.app.services.telegram_handlers.library_scene import _get_library_files

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _add(db_session, filename, file_type, *, sliced=None, deleted_at=None):
    meta = None if sliced is None else {SLICED_GCODE_META_KEY: sliced}
    f = LibraryFile(
        filename=filename,
        file_path=f"lib/{filename}",
        file_type=file_type,
        file_size=1,
        file_metadata=meta,
        deleted_at=deleted_at,
    )
    db_session.add(f)
    await db_session.commit()
    return f


async def _listed(test_engine, monkeypatch) -> set[str]:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    monkeypatch.setattr(
        "backend.app.core.database.async_session",
        async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False),
    )
    files, total = await _get_library_files()
    assert total == len(files), "the count and the page disagree"
    return {f.filename for f in files}


async def test_sliced_files_are_listed_and_models_are_not(db_session, test_engine, monkeypatch):
    await _add(db_session, "ready.gcode.3mf", "gcode")
    await _add(db_session, "Hot Air Deflector.3mf", "3mf")
    await _add(db_session, "bracket.stl", "stl")

    assert await _listed(test_engine, monkeypatch) == {"ready.gcode.3mf"}


async def test_a_model_named_like_a_sliced_file_is_not_offered(db_session, test_engine, monkeypatch):
    """The content flag overrules the name — the printer would refuse it."""
    await _add(db_session, "liar.gcode.3mf", "gcode", sliced=False)
    await _add(db_session, "real.gcode.3mf", "gcode", sliced=True)

    assert await _listed(test_engine, monkeypatch) == {"real.gcode.3mf"}


async def test_sliced_output_saved_without_the_compound_suffix_is_offered(db_session, test_engine, monkeypatch):
    await _add(db_session, "sliced-anyway.3mf", "3mf", sliced=True)

    assert await _listed(test_engine, monkeypatch) == {"sliced-anyway.3mf"}


async def test_rows_with_no_content_answer_stay_printable(db_session, test_engine, monkeypatch):
    """⚠️ An install that has not run the m137 backfill has no flag on any row.
    Treating unknown as "not printable" would empty the list entirely."""
    await _add(db_session, "legacy.gcode.3mf", "gcode", sliced=None)

    assert await _listed(test_engine, monkeypatch) == {"legacy.gcode.3mf"}


async def test_trashed_files_are_not_offered(db_session, test_engine, monkeypatch):
    """They were before: deleting a file in the web left it printable here."""
    from datetime import datetime, timezone

    await _add(db_session, "kept.gcode.3mf", "gcode")
    await _add(db_session, "binned.gcode.3mf", "gcode", deleted_at=datetime.now(timezone.utc))

    assert await _listed(test_engine, monkeypatch) == {"kept.gcode.3mf"}


async def test_raw_gcode_is_offered(db_session, test_engine, monkeypatch):
    """Not a 3MF container, so it carries no content flag — and it is still
    the most sliced thing there is."""
    await _add(db_session, "plain.gcode", "gcode")

    assert await _listed(test_engine, monkeypatch) == {"plain.gcode"}
