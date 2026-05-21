"""Regression tests for H.4 cluster (2) / upstream Bambuddy #1299.

``scan_external_folder`` defers STL / OBJ thumbnail generation to a
background task (``_backfill_external_mesh_thumbnails``) so the HTTP
request returns as soon as the folder / file rows are committed. Inline
generation held the request open for minutes on a large NAS mount and
the FE modal timed out before ``db.commit()`` — the original symptom
where subdirectories never showed up because nothing got committed.

The task opens its own session, processes one mesh file at a time, and
commits per-file. A bad mesh never aborts the rest of the batch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mesh_file(file_id: int, file_path: str = "library/files/a.stl") -> MagicMock:
    m = MagicMock()
    m.id = file_id
    m.file_path = file_path
    m.thumbnail_path = None
    return m


@pytest.mark.asyncio
async def test_no_folder_ids_is_noop():
    from backend.app.api.routes.library import _backfill_external_mesh_thumbnails

    # Should return before touching the DB / session at all.
    with patch("backend.app.core.database.async_session") as mock_session:
        await _backfill_external_mesh_thumbnails([])
        mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_backfills_thumbnail_and_commits_per_file():
    from backend.app.api.routes import library

    f1 = _mesh_file(1, "library/files/one.stl")
    f2 = _mesh_file(2, "library/files/two.obj")

    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [f1, f2]
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=db)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("backend.app.core.database.async_session", return_value=session_ctx),
        patch.object(library, "get_library_thumbnails_dir", return_value=Path("/thumbs")),
        patch.object(library, "to_absolute_path", side_effect=lambda p: Path("/abs") / Path(p).name),
        patch.object(Path, "exists", return_value=True),
        patch.object(library, "generate_stl_thumbnail", return_value="/thumbs/x.png"),
        patch.object(library, "to_relative_path", return_value="thumbnails/x.png"),
    ):
        await library._backfill_external_mesh_thumbnails([10, 11])

    # Both files got a thumbnail + a commit each.
    assert f1.thumbnail_path == "thumbnails/x.png"
    assert f2.thumbnail_path == "thumbnails/x.png"
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_one_bad_mesh_does_not_abort_the_rest():
    from backend.app.api.routes import library

    f1 = _mesh_file(1, "library/files/bad.stl")
    f2 = _mesh_file(2, "library/files/good.stl")

    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [f1, f2]
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=db)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    def _gen(abs_path, _thumbs):
        if "bad" in str(abs_path):
            raise RuntimeError("trimesh exploded")
        return "/thumbs/good.png"

    with (
        patch("backend.app.core.database.async_session", return_value=session_ctx),
        patch.object(library, "get_library_thumbnails_dir", return_value=Path("/thumbs")),
        patch.object(library, "to_absolute_path", side_effect=lambda p: Path("/abs") / Path(p).name),
        patch.object(Path, "exists", return_value=True),
        patch.object(library, "generate_stl_thumbnail", side_effect=_gen),
        patch.object(library, "to_relative_path", return_value="thumbnails/good.png"),
    ):
        await library._backfill_external_mesh_thumbnails([10])

    # Bad file left untouched; good file still got its thumbnail + commit.
    assert f1.thumbnail_path is None
    assert f2.thumbnail_path == "thumbnails/good.png"
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_missing_file_on_disk_skipped():
    from backend.app.api.routes import library

    f1 = _mesh_file(1, "library/files/gone.stl")

    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [f1]
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=db)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("backend.app.core.database.async_session", return_value=session_ctx),
        patch.object(library, "get_library_thumbnails_dir", return_value=Path("/thumbs")),
        patch.object(library, "to_absolute_path", side_effect=lambda p: Path("/abs") / Path(p).name),
        patch.object(Path, "exists", return_value=False),
        patch.object(library, "generate_stl_thumbnail") as mock_gen,
    ):
        await library._backfill_external_mesh_thumbnails([10])

    mock_gen.assert_not_called()
    assert f1.thumbnail_path is None
    db.commit.assert_not_awaited()
