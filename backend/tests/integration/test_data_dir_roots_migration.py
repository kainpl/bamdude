"""m177 end to end: files leave archive/ for their own roots and the four path
columns follow, in that order, and doing it again changes nothing.

``data_dir_isolation`` (conftest, autouse) points ``settings.data_dir`` at a
tmp directory, which is the one the migration reads."""

import pytest
from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.migrations import m177_data_dir_roots as m177
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta

pytestmark = pytest.mark.integration


def _put(rel: str) -> None:
    p = settings.data_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(rel.encode())


async def _run(engine):
    async with engine.begin() as conn:
        await m177.upgrade(conn)


@pytest.mark.asyncio
async def test_files_move_and_the_path_columns_follow_and_a_rerun_is_silent(db_session, test_engine):
    internal = LibraryFile(
        filename="a.3mf",
        file_path="archive\\library\\files\\a.3mf",
        thumbnail_path="archive/library/thumbnails/a.png",
        file_size=1,
        file_type="3mf",
    )
    external = LibraryFile(filename="n.3mf", file_path="\\\\nas\\share\\n.3mf", file_size=1, file_type="3mf")
    db_session.add_all([internal, external])
    await db_session.flush()
    db_session.add(
        LibraryFileMakerworldMeta(
            library_file_id=internal.id, cover_path="archive/library/makerworld-covers/1-cover.png"
        )
    )
    await db_session.commit()
    for rel in (
        "archive/library/files/a.3mf",
        "archive/library/thumbnails/a.png",
        "archive/library/makerworld-covers/1-cover.png",
        "archive/projects/3/attachments/x.jpg",
        "archive/products/4/attachments/y.jpg",
        "archive/1/run/print.3mf",
        "archive/temp/1/dl.3mf",
    ):
        _put(rel)

    await _run(test_engine)

    for rel in (
        "library/files/a.3mf",
        "library/thumbnails/a.png",
        "library/makerworld-covers/1-cover.png",
        "projects/3/attachments/x.jpg",
        "products/4/attachments/y.jpg",
        "archive/1/run/print.3mf",
        "archive/temp/1/dl.3mf",
    ):
        assert (settings.data_dir / rel).exists(), rel
    for gone in ("archive/library", "archive/projects", "archive/products"):
        assert not (settings.data_dir / gone).exists(), gone

    db_session.expire_all()
    rows = {r.filename: r for r in (await db_session.execute(select(LibraryFile))).scalars()}
    assert rows["a.3mf"].file_path == "library\\files\\a.3mf"
    assert rows["a.3mf"].thumbnail_path == "library/thumbnails/a.png"
    assert rows["n.3mf"].file_path == "\\\\nas\\share\\n.3mf"
    meta = (await db_session.execute(select(LibraryFileMakerworldMeta))).scalar_one()
    assert meta.cover_path == "library/makerworld-covers/1-cover.png"

    await _run(test_engine)
    db_session.expire_all()
    again = (await db_session.execute(select(LibraryFile).where(LibraryFile.filename == "a.3mf"))).scalar_one()
    assert again.file_path == "library\\files\\a.3mf"
    assert (settings.data_dir / "library/files/a.3mf").exists()
