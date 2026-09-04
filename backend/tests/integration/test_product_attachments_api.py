"""Product attachments (typed, ordered, four categories) and the cover rule.

Spec: docs/superpowers/specs/2026-09-04-projects-redesign-pass4-product-card-design.md
§Decisions 3 and 4.

``committing_client``, not ``async_client``: these handlers never commit —
production's ``get_db`` does it after the response.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app.services.product_files import product_attachments_dir

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
async def product(committing_client):
    return (await committing_client.post("/api/v1/products/", json={"name": "Lamp"})).json()["id"]


async def _upload(client, product_id: int, category: str, name: str, content: bytes = PNG):
    return await client.post(
        f"/api/v1/products/{product_id}/attachments",
        data={"category": category},
        files={"file": (name, content, "application/octet-stream")},
    )


async def _stream_token() -> str:
    from backend.app.core.auth import create_camera_stream_token

    return await create_camera_stream_token()


@pytest.mark.asyncio
async def test_every_category_takes_the_file_types_it_is_for(committing_client, product):
    for category, name in (
        ("pictures", "shot.png"),
        ("bom_docs", "bom.csv"),
        ("assembly", "guide.md"),
        ("other", "part.stl"),
    ):
        r = await _upload(committing_client, product, category, name)
        assert r.status_code == 200, f"{category}/{name}: {r.text}"
        entry = r.json()
        assert entry["category"] == category and entry["original_name"] == name
        assert entry["source"] == "manual" and entry["sort_order"] == 0
        assert entry["filename"].endswith(Path(name).suffix) and entry["filename"] != name
        assert (product_attachments_dir(product) / entry["filename"]).exists()

    listed = (await committing_client.get(f"/api/v1/products/{product}/attachments")).json()
    assert [a["category"] for a in listed] == ["pictures", "bom_docs", "assembly", "other"]

    # The download is bearer-authenticated and gives the file its own name back.
    stored = listed[1]["filename"]
    got = await committing_client.get(f"/api/v1/products/{product}/attachments/{stored}")
    assert got.status_code == 200 and "bom.csv" in got.headers["content-disposition"]


@pytest.mark.asyncio
async def test_each_category_refuses_what_it_does_not_carry(committing_client, product):
    """The allowlists are the only defence against uploading an executable."""
    for category, name in (
        ("pictures", "notes.pdf"),
        ("bom_docs", "shot.png"),
        ("assembly", "sheet.xlsx"),
        ("other", "run.exe"),
    ):
        r = await _upload(committing_client, product, category, name)
        assert r.status_code == 400, f"{category} accepted {name}"
    assert (await _upload(committing_client, product, "nonsense", "shot.png")).status_code == 400
    assert (await committing_client.get(f"/api/v1/products/{product}/attachments")).json() == []
    # And nothing reached the disk: the allowlist runs before the directory is made.
    directory = product_attachments_dir(product)
    assert not directory.exists() or list(directory.iterdir()) == []


@pytest.mark.asyncio
async def test_gallery_order_is_data_and_is_rewritten_per_category(committing_client, product):
    pics = [(await _upload(committing_client, product, "pictures", f"{i}.png")).json()["filename"] for i in range(3)]
    doc = (await _upload(committing_client, product, "bom_docs", "b.csv", b"a,b")).json()["filename"]
    assert [
        (await committing_client.get(f"/api/v1/products/{product}/attachments")).json()[i]["sort_order"]
        for i in range(3)
    ] == [0, 1, 2]

    r = await committing_client.patch(
        f"/api/v1/products/{product}/attachments/order",
        json={"category": "pictures", "filenames": [pics[2], pics[0], pics[1]]},
    )
    assert r.status_code == 200, r.text
    assert {a["filename"]: a["sort_order"] for a in r.json() if a["category"] == "pictures"} == {
        pics[2]: 0,
        pics[0]: 1,
        pics[1]: 2,
    }
    # The other category is untouched by a pictures reorder.
    assert [a["sort_order"] for a in r.json() if a["category"] == "bom_docs"] == [0]

    bad = await committing_client.patch(
        f"/api/v1/products/{product}/attachments/order",
        json={"category": "pictures", "filenames": [pics[0], doc]},
    )
    assert bad.status_code == 400, "a bom_docs filename is not the pictures gallery's to order"
    unknown = await committing_client.patch(
        f"/api/v1/products/{product}/attachments/order",
        json={"category": "nonsense", "filenames": []},
    )
    assert unknown.status_code == 400


@pytest.mark.asyncio
async def test_a_traversing_attachment_name_is_refused_before_the_path_join():
    """Called directly, not over HTTP, and deliberately so.

    ``{filename}`` is a plain path parameter, so Starlette never routes a name
    containing a separator to the handler at all — over the wire a traversal
    attempt is a 404 from the router and the guard inside is never reached.
    That makes the guard defence in depth, and the only way to show it still
    works is to call it. (Same test as the projects routes' twin.)
    """
    from backend.app.api.routes.products import (
        delete_attachment,
        download_attachment,
        get_attachment_image,
    )

    for handler in (download_attachment, delete_attachment, get_attachment_image):
        for name in ("../../secret.txt", "sub/file.txt", "..\\win.txt", ""):
            with pytest.raises(HTTPException) as raised:
                await handler(product_id=1, filename=name, db=None, _=None)
            assert raised.value.status_code == 400, f"{handler.__name__} accepted {name!r}"


@pytest.mark.asyncio
async def test_the_image_route_wants_a_stream_token_and_serves_pictures_only(committing_client, product):
    """``<img src>`` cannot carry an Authorization header, so pictures go out
    through the same ``?token=`` credential the project cover route takes."""
    pic = (await _upload(committing_client, product, "pictures", "a.png")).json()["filename"]
    doc = (await _upload(committing_client, product, "bom_docs", "b.csv", b"a,b")).json()["filename"]

    assert (await committing_client.get(f"/api/v1/products/{product}/attachment-image/{pic}")).status_code == 401
    token = await _stream_token()
    img = await committing_client.get(f"/api/v1/products/{product}/attachment-image/{pic}", params={"token": token})
    assert img.status_code == 200, img.text
    assert img.content == PNG and img.headers["content-type"] == "image/png"
    assert img.headers["cache-control"] == "private, max-age=3600"

    not_a_picture = await committing_client.get(
        f"/api/v1/products/{product}/attachment-image/{doc}", params={"token": token}
    )
    assert not_a_picture.status_code == 404


@pytest.mark.asyncio
async def test_delete_takes_the_file_the_entry_and_the_cover_that_pointed_at_it(committing_client, product):
    pic = (await _upload(committing_client, product, "pictures", "a.png")).json()["filename"]
    assert (
        await committing_client.put(f"/api/v1/products/{product}/cover-image", json={"filename": pic})
    ).status_code == 200
    assert (await committing_client.get(f"/api/v1/products/{product}")).json()["cover_image_filename"] == pic

    r = await committing_client.delete(f"/api/v1/products/{product}/attachments/{pic}")
    assert r.status_code == 200, r.text
    assert r.json() == []
    assert not (product_attachments_dir(product) / pic).exists()
    body = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert body["attachments"] == [] and body["cover_image_filename"] is None and body["has_cover"] is False
    assert (await committing_client.delete(f"/api/v1/products/{product}/attachments/{pic}")).status_code == 404


@pytest.mark.asyncio
async def test_a_dedicated_cover_is_stored_beside_the_gallery_but_never_in_it(committing_client, product):
    doc = (await _upload(committing_client, product, "bom_docs", "b.csv", b"a,b")).json()["filename"]
    picked = await committing_client.put(f"/api/v1/products/{product}/cover-image", json={"filename": doc})
    assert picked.status_code == 400, "only a picture can be the cover"

    up = await committing_client.put(
        f"/api/v1/products/{product}/cover-image", files={"file": ("c.png", PNG, "image/png")}
    )
    assert up.status_code == 200, up.text
    first = up.json()["filename"]
    assert first.startswith("cover_") and first.endswith(".png")

    detail = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert detail["cover_image_filename"] == first and detail["has_cover"] is True
    assert [a["filename"] for a in detail["attachments"]] == [doc], "the dedicated cover is not a gallery entry"

    token = await _stream_token()
    img = await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    assert img.status_code == 200 and img.content == PNG

    # A replacement takes the file it replaces with it — nothing else references it.
    second = (
        await committing_client.put(
            f"/api/v1/products/{product}/cover-image", files={"file": ("d.png", PNG, "image/png")}
        )
    ).json()["filename"]
    assert not (product_attachments_dir(product) / first).exists()
    assert (product_attachments_dir(product) / second).exists()

    bad_type = await committing_client.put(
        f"/api/v1/products/{product}/cover-image", files={"file": ("c.svg", b"<svg/>", "image/svg+xml")}
    )
    assert bad_type.status_code == 400


@pytest.mark.asyncio
async def test_the_cover_falls_back_to_the_first_picture_in_gallery_order(committing_client, product):
    a = (await _upload(committing_client, product, "pictures", "a.png", PNG + b"AAA")).json()["filename"]
    b = (await _upload(committing_client, product, "pictures", "b.png", PNG + b"BBB")).json()["filename"]
    token = await _stream_token()

    detail = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert detail["cover_image_filename"] is None and detail["has_cover"] is True
    assert (
        await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    ).content == PNG + b"AAA"

    await committing_client.patch(
        f"/api/v1/products/{product}/attachments/order", json={"category": "pictures", "filenames": [b, a]}
    )
    assert (
        await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    ).content == PNG + b"BBB"

    # An explicit pick wins; clearing it lets the default resume, and it does
    # NOT delete the gallery picture it was pointing at.
    await committing_client.put(f"/api/v1/products/{product}/cover-image", json={"filename": a})
    assert (
        await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    ).content == PNG + b"AAA"
    assert (await committing_client.delete(f"/api/v1/products/{product}/cover-image")).status_code == 200
    assert (product_attachments_dir(product) / a).exists()
    assert (
        await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    ).content == PNG + b"BBB"


@pytest.mark.asyncio
async def test_a_dangling_cover_column_heals_to_null(committing_client, product):
    """And the heal survives the 404 — ``get_db`` rolls back on anything that
    escapes the handler, so the route returns the 404 instead of raising it."""
    name = (
        await committing_client.put(
            f"/api/v1/products/{product}/cover-image", files={"file": ("c.png", PNG, "image/png")}
        )
    ).json()["filename"]
    (product_attachments_dir(product) / name).unlink()

    token = await _stream_token()
    gone = await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    assert gone.status_code == 404
    detail = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert detail["cover_image_filename"] is None and detail["has_cover"] is False


@pytest.mark.asyncio
async def test_has_cover_is_the_effective_cover_in_list_and_detail(committing_client, product):
    def row(rows):
        return next(p for p in rows if p["id"] == product)

    assert (await committing_client.get(f"/api/v1/products/{product}")).json()["has_cover"] is False
    assert row((await committing_client.get("/api/v1/products/")).json())["has_cover"] is False

    await _upload(committing_client, product, "pictures", "a.png")
    assert (await committing_client.get(f"/api/v1/products/{product}")).json()["has_cover"] is True
    assert row((await committing_client.get("/api/v1/products/")).json())["has_cover"] is True

    # A bom_docs attachment is not a cover.
    other = (await committing_client.post("/api/v1/products/", json={"name": "Other"})).json()["id"]
    await _upload(committing_client, other, "bom_docs", "b.csv", b"a,b")
    assert (await committing_client.get(f"/api/v1/products/{other}")).json()["has_cover"] is False


@pytest.mark.asyncio
async def test_the_browser_reaches_the_picture_with_only_a_stream_token(committing_client, product):
    """Issued from an UNAUTHENTICATED client, and that is the whole point.

    ``main.py``'s ``auth_middleware`` runs BEFORE any route dependency, so a
    token-gated ``<img>`` route answers 401 from the middleware — never reaching
    its own ``RequireCameraStreamToken`` — unless its path matches an entry in
    ``PUBLIC_API_PATTERNS``. The ``committing_client`` fixture carries an admin
    JWT and sails past that middleware, so it cannot see the bug at all: it
    would report a green ``<img>`` route no browser can load. That is exactly
    how ``/attachments/{filename}/image`` shipped, and why the picture route is
    now ``/attachment-image/{filename}`` with its own whitelist entry.
    """
    from httpx import ASGITransport, AsyncClient

    from backend.app.main import app

    pic = (await _upload(committing_client, product, "pictures", "a.png")).json()["filename"]
    await committing_client.put(f"/api/v1/products/{product}/cover-image", json={"filename": pic})
    token = await _stream_token()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        picture = f"/api/v1/products/{product}/attachment-image/{pic}"
        assert (await anonymous.get(picture)).status_code == 401, "no token is still no entry"
        served = await anonymous.get(picture, params={"token": token})
        assert served.status_code == 200, served.text
        assert served.content == PNG

        # The cover route is reachable the same way (``/cover`` is already a
        # whitelisted pattern, as it is for the project cover route).
        cover = await anonymous.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
        assert cover.status_code == 200 and cover.content == PNG
        assert (await anonymous.get(f"/api/v1/products/{product}/cover-image")).status_code == 401

        # The whitelist entry is narrow: the bearer-only download beside it, and
        # the ordinary reads, are still 401 for a client with no session.
        assert (await anonymous.get(f"/api/v1/products/{product}/attachments/{pic}")).status_code == 401
        assert (await anonymous.get(f"/api/v1/products/{product}/attachments")).status_code == 401
        assert (await anonymous.get(f"/api/v1/products/{product}")).status_code == 401


@pytest.mark.asyncio
async def test_unknown_names_and_unknown_products_are_404(committing_client, product):
    ghost = "deadbeefdeadbeefdeadbeefdeadbeef.png"
    assert (await committing_client.get(f"/api/v1/products/{product}/attachments/{ghost}")).status_code == 404
    assert (await committing_client.delete(f"/api/v1/products/{product}/attachments/{ghost}")).status_code == 404
    token = await _stream_token()
    assert (
        await committing_client.get(f"/api/v1/products/{product}/attachment-image/{ghost}", params={"token": token})
    ).status_code == 404
    assert (await committing_client.get("/api/v1/products/9999/attachments")).status_code == 404
    assert (await _upload(committing_client, 9999, "pictures", "a.png")).status_code == 404
    assert (
        await committing_client.patch(
            "/api/v1/products/9999/attachments/order", json={"category": "pictures", "filenames": []}
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_deleting_the_implicit_cover_hands_the_role_to_the_next_picture(committing_client, product):
    """No cover column is set anywhere in this test: the cover is whichever
    picture is first by ``sort_order``, so deleting it must promote the next one
    and deleting the last must leave the product with no cover at all."""
    first = (await _upload(committing_client, product, "pictures", "a.png", PNG + b"AAA")).json()["filename"]
    second = (await _upload(committing_client, product, "pictures", "b.png", PNG + b"BBB")).json()["filename"]
    token = await _stream_token()

    detail = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert detail["cover_image_filename"] is None and detail["has_cover"] is True

    assert (await committing_client.delete(f"/api/v1/products/{product}/attachments/{first}")).status_code == 200
    detail = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert detail["cover_image_filename"] is None and detail["has_cover"] is True
    assert (
        await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    ).content == PNG + b"BBB"

    assert (await committing_client.delete(f"/api/v1/products/{product}/attachments/{second}")).status_code == 200
    detail = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert detail["has_cover"] is False
    assert (
        await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    ).status_code == 404


@pytest.mark.asyncio
async def test_an_implicit_cover_whose_file_vanished_prunes_that_picture(committing_client, product):
    """The heal has to cover BOTH shapes of the cover rule.

    The explicit branch clears a column. The implicit one has no column to
    clear: the first picture elected itself, so the entry IS the thing that is
    wrong, and leaving it would make ``has_cover`` keep promising a picture
    every request 404s on — with the next picture hidden behind it forever.
    """
    first = (await _upload(committing_client, product, "pictures", "a.png", PNG + b"AAA")).json()["filename"]
    second = (await _upload(committing_client, product, "pictures", "b.png", PNG + b"BBB")).json()["filename"]
    (product_attachments_dir(product) / first).unlink()

    token = await _stream_token()
    gone = await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    assert gone.status_code == 404

    detail = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert [a["filename"] for a in detail["attachments"]] == [second]
    assert detail["cover_image_filename"] is None and detail["has_cover"] is True
    assert (
        await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    ).content == PNG + b"BBB"


@pytest.mark.asyncio
async def test_the_cover_is_revalidated_and_a_uuid_named_picture_is_not(committing_client, product):
    """Two URLs, two caching answers, and the difference is whether the URL is
    stable across the bytes changing.

    ``/cover-image`` is one URL for whatever the cover happens to be, so a
    browser that reuses a heuristically fresh copy shows the OLD picture after
    an upload — ``no-cache`` means revalidate, not "do not store".
    ``/attachment-image/<uuid>.png`` can never change under its own name.
    """
    name = (await _upload(committing_client, product, "pictures", "a.png")).json()["filename"]
    token = await _stream_token()

    cover = await committing_client.get(f"/api/v1/products/{product}/cover-image", params={"token": token})
    assert cover.headers["cache-control"] == "private, no-cache"

    picture = await committing_client.get(
        f"/api/v1/products/{product}/attachment-image/{name}", params={"token": token}
    )
    assert picture.headers["cache-control"] == "private, max-age=3600"


@pytest.mark.asyncio
async def test_a_download_names_the_file_in_the_operators_own_alphabet(committing_client, product):
    """``FileResponse(filename=...)`` is encoded latin-1, so a Ukrainian name
    would have raised ``UnicodeEncodeError`` from inside the response instead of
    downloading. Same RFC 6266 helper as ``card-download`` and the export."""
    name = (await _upload(committing_client, product, "bom_docs", "специфікація.csv", b"a,b\n")).json()["filename"]

    r = await committing_client.get(f"/api/v1/products/{product}/attachments/{name}")

    assert r.status_code == 200 and r.content == b"a,b\n"
    # The legacy ``filename=`` parameter keeps whatever survives latin-1 — here
    # the extension alone, stripped of its leading dot — and the real name
    # travels in ``filename*``, which every modern browser prefers.
    assert r.headers["content-disposition"] == (
        "attachment; filename=\"csv\"; filename*=UTF-8''%D1%81%D0%BF%D0%B5%D1%86%D0%B8%D1%84%D1%96%D0%BA%D0%B0%D1%86%D1%96%D1%8F.csv"
    )


@pytest.mark.asyncio
async def test_a_legacy_row_missing_its_category_renders_instead_of_500ing(committing_client, db_session, product):
    """m158 carried project attachments over into this column, and a restored
    backup can hold anything. ``ProductAttachmentOut`` defaults every field a
    legacy row may lack — ``filename`` alone has no default, and an entry
    without one is dropped by ``_rows`` long before the model sees it."""
    from sqlalchemy import select

    from backend.app.models.product import Product

    row = (await db_session.execute(select(Product).where(Product.id == product))).scalar_one()
    row.attachments = [{"filename": "legacy.png", "size": 3}, {"original_name": "nameless.png"}]
    await db_session.commit()

    detail = (await committing_client.get(f"/api/v1/products/{product}")).json()

    assert detail["attachments"] == [
        {
            "category": "other",
            "filename": "legacy.png",
            "original_name": "",
            "size": 3,
            "sort_order": 0,
            "source": "manual",
            "source_file_id": None,
            "uploaded_at": None,
        }
    ]
