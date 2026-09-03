"""The read-only model card of a LIBRARY file (spec §Decisions 5).

Two routes, and the difference between them is the whole point:

* ``GET /library/files/{id}/card`` is an ordinary ownership-scoped library read
  — it answers JSON, so it can take the bearer token every other library read
  takes.
* ``GET /library/files/{id}/card-file/{zip_path}`` feeds an ``<img src>``,
  which cannot carry an Authorization header, so it takes the same ``?token=``
  stream credential the cover routes take.

⚠️ The second route hands out bytes from inside a file the operator uploaded,
addressed by a path the CLIENT supplies. It therefore serves **only** a member
the parsed card listed under ``Auxiliaries/`` — never ``3D/3dmodel.model``,
never the sliced G-code, never a name that merely looks like one. That
restriction is pinned below and must not be relaxed into "any ZIP member".

⚠️ Nothing here writes into the 3MF. A library file is the operator's original
(spec §Risks); the card is read and shown, never saved back.

``write_card_3mf`` is shared with ``test_products_api.py`` — one builder, so the
auto-fill tests and the card tests cannot drift about what a card 3MF contains.
"""

import zipfile
from pathlib import Path

import pytest

from backend.app.models.library import LibraryFile

pytestmark = pytest.mark.integration

# Named payloads: every size assertion below reads as "what we wrote".
PNG_A = b"\x89PNG-a"
JPG_B = b"jpeg-bytes-b"
BOM_CSV = b"part,qty\n"
GUIDE_PDF = b"%PDF-1.4 guide"
NOTES_TXT = b"notes"
EVIL_EXE = b"MZ evil"
PNG_PROFILE = b"\x89PNG-p"
PNG_THUMB = b"\x89PNG-t"

# The four designer folders a product imports, plus the two BambuStudio ones
# that stay out of the product but still belong to the card.
DEFAULT_MEMBERS: dict[str, bytes] = {
    "Auxiliaries/Model Pictures/a.png": PNG_A,
    "Auxiliaries/Model Pictures/b.jpg": JPG_B,
    "Auxiliaries/Bill of Materials/bom.csv": BOM_CSV,
    "Auxiliaries/Assembly Guide/guide.pdf": GUIDE_PDF,
    "Auxiliaries/Others/notes.txt": NOTES_TXT,
    "Auxiliaries/Profile Pictures/p.png": PNG_PROFILE,
    "Auxiliaries/.thumbnails/t.png": PNG_THUMB,
}

# `&amp;amp;amp;` is BambuStudio's observed triple encoding — the card must come
# back with a single `&`, in the product columns as much as on the card screen.
_METADATA_KEYS = {
    "title": "Title",
    "description": "Description",
    "designer": "Designer",
    "license": "License",
    "design_model_id": "DesignModelId",
}


def write_card_3mf(
    path: Path,
    *,
    title: str | None = "Desk Lamp",
    description: str | None = "A lamp &amp;amp;amp; a shade",
    designer: str | None = "Chef&amp;amp;amp;koch",
    license: str | None = "CC-BY-4.0",
    design_model_id: str | None = "1234567",
    members: dict[str, bytes] | None = None,
) -> Path:
    """A 3MF carrying a model card. ``members`` replaces the auxiliaries wholesale."""
    values = {
        "title": title,
        "description": description,
        "designer": designer,
        "license": license,
        "design_model_id": design_model_id,
    }
    metadata = "".join(
        f'<metadata name="{_METADATA_KEYS[key]}">{value}</metadata>' for key, value in values.items() if value
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", f'<?xml version="1.0"?><model>{metadata}</model>')
        for name, payload in (DEFAULT_MEMBERS if members is None else members).items():
            zf.writestr(name, payload)
    return path


async def make_card_file(db, tmp_path: Path, *, name: str = "lamp.3mf", **card) -> LibraryFile:
    """A library row whose bytes are really on disk under ``settings.base_dir``.

    ``data_dir_isolation`` points ``base_dir`` at ``tmp_path``, so a relative
    ``file_path`` resolves exactly the way production's does.
    """
    relative = f"library/{name}"
    written = write_card_3mf(tmp_path / relative, **card)
    row = LibraryFile(
        filename=name,
        file_path=relative,
        file_size=written.stat().st_size,
        file_type="3mf",
        file_metadata={"plates": [{"index": 1, "printable_objects": {"1": "shade.stl"}, "print_time_seconds": 5}]},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _stream_token() -> str:
    from backend.app.core.auth import create_camera_stream_token

    return await create_camera_stream_token()


@pytest.mark.asyncio
async def test_the_card_is_parsed_off_disk_with_token_gated_urls(committing_client, db_session, tmp_path):
    """``file_metadata`` carries only ``designer`` and ``print_name``; the card
    is the 3MF's own, so it is read from the file every time it is asked for."""
    file_id = (await make_card_file(db_session, tmp_path)).id

    r = await committing_client.get(f"/api/v1/library/files/{file_id}/card")
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["title"] == "Desk Lamp" and card["designer"] == "Chef&koch"
    assert card["description"] == "A lamp & a shade"
    assert card["license"] == "CC-BY-4.0" and card["design_model_id"] == "1234567"
    assert card["error"] is None

    aux = card["auxiliaries"]
    assert sorted(aux) == ["assembly", "bom_docs", "other", "pictures", "profile_pictures", "thumbnails"]
    assert [e["name"] for e in aux["pictures"]] == ["a.png", "b.jpg"]
    assert [e["name"] for e in aux["bom_docs"]] == ["bom.csv"]
    assert [e["name"] for e in aux["assembly"]] == ["guide.pdf"]
    assert [e["name"] for e in aux["other"]] == ["notes.txt"]
    assert [e["name"] for e in aux["profile_pictures"]] == ["p.png"]
    assert [e["name"] for e in aux["thumbnails"]] == ["t.png"]

    first = aux["pictures"][0]
    assert first["zip_path"] == "Auxiliaries/Model Pictures/a.png" and first["size"] == len(PNG_A)
    assert first["url"] == f"/api/v1/library/files/{file_id}/card-file/Auxiliaries/Model%20Pictures/a.png"

    # The url is the token-gated route, and it really is gated.
    assert (await committing_client.get(first["url"])).status_code == 401
    token = await _stream_token()
    got = await committing_client.get(first["url"], params={"token": token})
    assert got.status_code == 200, got.text
    assert got.content == PNG_A and got.headers["content-type"] == "image/png"

    # And the same route serves the documents the card lists, with their own type.
    bom = await committing_client.get(aux["bom_docs"][0]["url"], params={"token": token})
    assert bom.status_code == 200 and bom.content == BOM_CSV and bom.headers["content-type"].startswith("text/csv")


@pytest.mark.asyncio
async def test_card_file_serves_only_what_the_card_listed(committing_client, db_session, tmp_path):
    """The client names the ZIP member, so the card's own listing is the allowlist.

    Without it this route is an arbitrary read of every file an operator ever
    uploaded — the model mesh, the sliced G-code, anything a crafted 3MF hides
    beside them.
    """
    file_id = (await make_card_file(db_session, tmp_path)).id
    token = await _stream_token()

    for member in (
        "3D/3dmodel.model",  # present in the ZIP, absent from the card
        "Metadata/Slic3r_PE.config",
        "Auxiliaries/Model Pictures/missing.png",
        "Auxiliaries/",
    ):
        r = await committing_client.get(f"/api/v1/library/files/{file_id}/card-file/{member}", params={"token": token})
        assert r.status_code == 404, f"{member} was served: {r.status_code}"

    assert (await committing_client.get("/api/v1/library/files/999999/card")).status_code == 404
    missing = await committing_client.get(
        "/api/v1/library/files/999999/card-file/Auxiliaries/Model Pictures/a.png", params={"token": token}
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_a_file_without_bytes_or_without_a_card_does_not_500(committing_client, db_session, tmp_path):
    """``parse()`` never raises (see ``test_threemf_card.py``), so a 3MF that is
    not one comes back as a card carrying ``error`` — the screen degrades, the
    request does not fail."""
    broken = tmp_path / "library" / "broken.3mf"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not a zip at all")
    row = LibraryFile(filename="broken.3mf", file_path="library/broken.3mf", file_size=16, file_type="3mf")
    ghost = LibraryFile(filename="ghost.3mf", file_path="library/ghost.3mf", file_size=1, file_type="3mf")
    db_session.add_all([row, ghost])
    await db_session.commit()
    broken_id, ghost_id = row.id, ghost.id

    r = await committing_client.get(f"/api/v1/library/files/{broken_id}/card")
    assert r.status_code == 200, r.text
    assert r.json()["error"] and r.json()["auxiliaries"]["pictures"] == []

    # A row whose bytes are gone is a 404, the same answer the plate routes give.
    assert (await committing_client.get(f"/api/v1/library/files/{ghost_id}/card")).status_code == 404


@pytest.mark.asyncio
async def test_the_browser_reaches_card_file_with_only_a_stream_token(committing_client, db_session, tmp_path):
    """Issued from an UNAUTHENTICATED client, and that is the whole point.

    ``main.py``'s ``auth_middleware`` runs BEFORE any route dependency, so a
    token-gated ``<img>`` route answers 401 from the middleware — never reaching
    its own ``RequireCameraStreamToken`` — unless its path matches an entry in
    ``PUBLIC_API_PATTERNS``. The ``committing_client`` fixture carries an admin
    JWT and sails past that middleware, so it cannot see the bug at all: it
    would report a green ``<img>`` route no browser can load.
    """
    from httpx import ASGITransport, AsyncClient

    from backend.app.main import app

    file_id = (await make_card_file(db_session, tmp_path)).id
    picture = f"/api/v1/library/files/{file_id}/card-file/Auxiliaries/Model Pictures/a.png"
    token = await _stream_token()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        assert (await anonymous.get(picture)).status_code == 401, "no token is still no entry"
        served = await anonymous.get(picture, params={"token": token})
        assert served.status_code == 200, served.text
        assert served.content == PNG_A

        # The card itself is an ordinary bearer read and stays behind the JWT.
        assert (await anonymous.get(f"/api/v1/library/files/{file_id}/card")).status_code == 401
