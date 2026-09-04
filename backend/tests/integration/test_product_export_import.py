"""Product export/import — a ZIP keyed by file hash (spec §Decisions 6).

The round trip is the test: a product with a directly-linked file, a
folder-linked file, one attachment per category and an explicit cover goes out
as a ZIP and comes back as the same product — with the library deciding which
rows the files become.

⚠️ The two files exist to exercise the two import branches, and the test only
proves anything because it destroys the source in between: the product is
deleted and ONE of the two files is trashed. The surviving file must be matched
by hash against its existing row (no new row); the trashed one must be ingested
afresh (``find_reusable_row`` ignores trashed siblings — a file the user deleted
must not pin an arrival to itself).

``committing_client``, not ``async_client``: the product handlers never commit —
production's ``get_db`` does it after the response.
"""

import hashlib
import io
import json
import pathlib
import struct
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import ProductPlate
from backend.app.services.product_files import product_attachments_dir
from backend.app.utils.http import build_content_disposition

pytestmark = pytest.mark.integration


def _codes(warnings: list[dict]) -> list[str]:
    return [w["code"] for w in warnings]


def _params(warnings: list[dict], code: str) -> list[dict]:
    """The params of every note with this code.

    Warnings are ``CardNote`` codes, never prose, so a test asserts on the code
    and its data — never on a sentence the frontend is going to replace with a
    translation.
    """
    return [w["params"] for w in warnings if w["code"] == code]


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _temp_exports() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("bamdude-product-export-*.zip"))


PNG = b"\x89PNG\r\n\x1a\nshot"
BOM = b"part,qty\nhook,1\n"
GUIDE = b"%PDF-1.4 assembly"
COVER = b"\x89PNG\r\n\x1a\ncover"


def sliced_3mf(objects_by_plate: dict[int, list[str]], *, marker: bytes = b"") -> bytes:
    """A real sliced 3MF: ``plate_N.gcode`` for plate discovery, ``slice_info``
    for the object names the composition sync seeds parts from.

    ``marker`` only exists to make two otherwise identical files hash apart.
    """
    identify = 100
    plates = []
    for index, names in sorted(objects_by_plate.items()):
        objects = ""
        for name in names:
            identify += 1
            objects += f'<object identify_id="{identify}" name="{name}" skipped="false" />'
        plates.append(
            f'<plate><metadata key="index" value="{index}" /><metadata key="prediction" value="600" />{objects}</plate>'
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", '<?xml version="1.0"?><model/>')
        zf.writestr("Metadata/slice_info.config", '<?xml version="1.0"?><config>' + "".join(plates) + "</config>")
        for index in sorted(objects_by_plate):
            zf.writestr(f"Metadata/plate_{index}.gcode", b"; sliced\n" + marker)
    return buf.getvalue()


DIRECT = sliced_3mf({1: ["hook.stl", "hook.stl"], 2: ["clip.stl"]}, marker=b"direct")
SHARED = sliced_3mf({1: ["shade.stl"]}, marker=b"shared")


async def _upload(client, name: str, content: bytes, folder_id: int | None = None) -> dict:
    url = "/api/v1/library/files" + (f"?folder_id={folder_id}" if folder_id is not None else "")
    r = await client.post(url, files={"file": (name, content, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


async def _attach(client, product_id: int, category: str, name: str, content: bytes) -> dict:
    r = await client.post(
        f"/api/v1/products/{product_id}/attachments",
        data={"category": category},
        files={"file": (name, content, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _active_files(db) -> int:
    return (
        await db.execute(select(func.count()).select_from(LibraryFile).where(LibraryFile.deleted_at.is_(None)))
    ).scalar()


def _manifest(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return json.loads(zf.read("product.json"))


def _rebuild(data: bytes, *, manifest: dict | None = None, extra: dict[str, bytes] | None = None) -> bytes:
    """The same archive with a different manifest and/or an extra member."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for name in source.namelist():
            if name == "product.json" and manifest is not None:
                continue
            target.writestr(name, source.read(name))
        if manifest is not None:
            target.writestr("product.json", json.dumps(manifest))
        for name, payload in (extra or {}).items():
            target.writestr(name, payload)
    return out.getvalue()


@pytest.fixture
async def exported(committing_client, db_session):
    """A fully furnished product, its export, and the state to import into.

    Returns ``(zip bytes, manifest, {"shared_file_id", "direct_file_id"})``. The
    product is deleted and the direct file trashed before the fixture returns, so
    every import test starts from the same wreckage.
    """
    folder = (await committing_client.post("/api/v1/library/folders", json={"name": "Lamp parts"})).json()
    shared = await _upload(committing_client, "shade.gcode.3mf", SHARED, folder_id=folder["id"])
    direct = await _upload(committing_client, "lamp.gcode.3mf", DIRECT)

    product = (await committing_client.post("/api/v1/products/", json={"name": "Desk Lamp"})).json()
    pid = product["id"]
    await committing_client.patch(
        f"/api/v1/products/{pid}",
        json={
            "description": "A lamp & a shade",
            "notes": "<p>keep the diffuser</p>",
            "designer": "Chef&koch",
            "license": "CC-BY-4.0",
            "source_url": "https://makerworld.com/models/1234567",
            "design_id": "1234567",
        },
    )
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/files", json={"library_file_ids": [direct["id"]]})
    ).status_code == 200
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/folders", json={"library_folder_ids": [folder["id"]]})
    ).status_code == 200

    detail = (await committing_client.get(f"/api/v1/products/{pid}")).json()
    parts = {p["name_key"]: p for p in detail["parts"]}
    assert set(parts) == {"hook.stl", "clip.stl", "shade.stl"}, detail["parts"]
    await committing_client.patch(f"/api/v1/products/{pid}/parts/{parts['hook.stl']['id']}", json={"qty_per_unit": 4})
    await committing_client.post(
        f"/api/v1/products/{pid}/parts",
        json={"kind": "purchased", "name": "M3 screw", "qty_per_unit": 8, "unit_price": 0.05},
    )

    shot = await _attach(committing_client, pid, "pictures", "shot.png", PNG)
    await _attach(committing_client, pid, "bom_docs", "bom.csv", BOM)
    await _attach(committing_client, pid, "assembly", "guide.pdf", GUIDE)
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/cover-image", json={"filename": shot["filename"]})
    ).status_code == 200

    r = await committing_client.get(f"/api/v1/products/{pid}/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    data = r.content

    assert (await committing_client.delete(f"/api/v1/products/{pid}")).status_code == 200
    assert (await committing_client.delete(f"/api/v1/library/files/{direct['id']}")).status_code == 200
    return data, _manifest(data), {"shared_file_id": shared["id"], "direct_file_id": direct["id"]}


async def _import(client, data: bytes, **form):
    return await client.post(
        "/api/v1/products/import",
        data=form,
        files={"file": ("product.zip", data, "application/zip")},
    )


async def _import_chunked(client, data: bytes):
    """The same POST with no ``Content-Length``.

    httpx computes one from ``files=``, so the declared-length gate would always
    fire first and the size check on the spooled part would never be exercised.
    An async iterator makes httpx send ``Transfer-Encoding: chunked`` instead,
    which is also what a real streaming client does.
    """
    boundary = "bamdudeimportboundary"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="product.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )

    async def stream():
        yield body

    return await client.post(
        "/api/v1/products/import",
        content=stream(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def _corrupt_member(data: bytes, member: str) -> bytes:
    """The same archive with one byte of one member flipped.

    Rebuilt with the default ``ZIP_STORED`` so the payload is verbatim: flipping
    a byte then leaves the length and every header intact and fails exactly one
    check, that member's CRC — which ``ZipFile()`` and ``namelist()`` say nothing
    about and ``read()`` raises on. That is the shape of the accident this
    guards against: an archive that opens, validates, and blows up half-way
    through the copying.

    ⚠️ The data offset is read from the LOCAL header, never computed from the
    central directory's ``extra``. The two extra fields are allowed to differ in
    length, so ``header_offset + 30 + len(filename) + len(extra)`` lands
    somewhere else in the file — which corrupted a header instead, made the
    import fail at ``ZipFile()`` before it had written anything, and left this
    test passing while proving nothing.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
    raw = bytearray(out.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as probe:
        offset = probe.getinfo(member).header_offset
    name_len, extra_len = struct.unpack("<HH", raw[offset + 26 : offset + 30])
    raw[offset + 30 + name_len + extra_len] ^= 0xFF
    return bytes(raw)


def _attachment_files() -> set[pathlib.Path]:
    """Every file under ``<archive_dir>/products/``, whoever it belongs to.

    ⚠️ Not "directories that appeared", which is what an earlier version of this
    counted: SQLite hands a deleted row's id straight back, so an import that
    follows a delete lands in the SAME ``products/<id>/attachments`` the previous
    product used, and a new-directory check sees nothing at all. What a failed
    import must leave behind is not "no new directory" but "not one new file".
    """
    from backend.app.core.config import settings

    root = pathlib.Path(settings.archive_dir) / "products"
    return {p for p in root.rglob("*") if p.is_file()} if root.is_dir() else set()


@pytest.fixture
async def exported(committing_client, db_session):
    """A fully furnished product, its export, and the state to import into.

    Returns ``(zip bytes, manifest, {"shared_file_id", "direct_file_id"})``. The
    product is deleted and the direct file trashed before the fixture returns, so
    every import test starts from the same wreckage.
    """
    folder = (await committing_client.post("/api/v1/library/folders", json={"name": "Lamp parts"})).json()
    shared = await _upload(committing_client, "shade.gcode.3mf", SHARED, folder_id=folder["id"])
    direct = await _upload(committing_client, "lamp.gcode.3mf", DIRECT)

    product = (await committing_client.post("/api/v1/products/", json={"name": "Desk Lamp"})).json()
    pid = product["id"]
    await committing_client.patch(
        f"/api/v1/products/{pid}",
        json={
            "description": "A lamp & a shade",
            "notes": "<p>keep the diffuser</p>",
            "designer": "Chef&koch",
            "license": "CC-BY-4.0",
            "source_url": "https://makerworld.com/models/1234567",
            "design_id": "1234567",
        },
    )
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/files", json={"library_file_ids": [direct["id"]]})
    ).status_code == 200
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/folders", json={"library_folder_ids": [folder["id"]]})
    ).status_code == 200

    detail = (await committing_client.get(f"/api/v1/products/{pid}")).json()
    parts = {p["name_key"]: p for p in detail["parts"]}
    assert set(parts) == {"hook.stl", "clip.stl", "shade.stl"}, detail["parts"]
    await committing_client.patch(f"/api/v1/products/{pid}/parts/{parts['hook.stl']['id']}", json={"qty_per_unit": 4})
    await committing_client.post(
        f"/api/v1/products/{pid}/parts",
        json={"kind": "purchased", "name": "M3 screw", "qty_per_unit": 8, "unit_price": 0.05},
    )

    shot = await _attach(committing_client, pid, "pictures", "shot.png", PNG)
    await _attach(committing_client, pid, "bom_docs", "bom.csv", BOM)
    await _attach(committing_client, pid, "assembly", "guide.pdf", GUIDE)
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/cover-image", json={"filename": shot["filename"]})
    ).status_code == 200

    r = await committing_client.get(f"/api/v1/products/{pid}/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    data = r.content

    assert (await committing_client.delete(f"/api/v1/products/{pid}")).status_code == 200
    assert (await committing_client.delete(f"/api/v1/library/files/{direct['id']}")).status_code == 200
    return data, _manifest(data), {"shared_file_id": shared["id"], "direct_file_id": direct["id"]}


async def _import(client, data: bytes, **form):
    return await client.post(
        "/api/v1/products/import",
        data=form,
        files={"file": ("product.zip", data, "application/zip")},
    )


async def _import_chunked(client, data: bytes):
    """The same POST with no ``Content-Length``.

    httpx computes one from ``files=``, so the declared-length gate would always
    fire first and the size check on the spooled part would never be exercised.
    An async iterator makes httpx send ``Transfer-Encoding: chunked`` instead,
    which is also what a real streaming client does.
    """
    boundary = "bamdudeimportboundary"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="product.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )

    async def stream():
        yield body

    return await client.post(
        "/api/v1/products/import",
        content=stream(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def _corrupt_member(data: bytes, member: str) -> bytes:
    """The same archive with one byte of one member flipped.

    Rebuilt with the default ``ZIP_STORED`` so the payload is verbatim: flipping
    a byte then leaves the length and every header intact and fails exactly one
    check, that member's CRC — which ``ZipFile()`` and ``namelist()`` say nothing
    about and ``read()`` raises on. That is the shape of the accident this
    guards against: an archive that opens, validates, and blows up half-way
    through the copying.

    ⚠️ The data offset is read from the LOCAL header, never computed from the
    central directory's ``extra``. The two extra fields are allowed to differ in
    length, so ``header_offset + 30 + len(filename) + len(extra)`` lands
    somewhere else in the file — which corrupted a header instead, made the
    import fail at ``ZipFile()`` before it had written anything, and left this
    test passing while proving nothing.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
    raw = bytearray(out.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as probe:
        offset = probe.getinfo(member).header_offset
    name_len, extra_len = struct.unpack("<HH", raw[offset + 26 : offset + 30])
    raw[offset + 30 + name_len + extra_len] ^= 0xFF
    return bytes(raw)


def _product_dirs() -> dict[str, Path]:
    """``{product id: its attachments directory}``.

    Keyed on the ID, not on the directory's own name — every one of them is
    called "attachments", so a set of names is a set of one and an orphan check
    built on it asserts nothing at all.
    """
    from backend.app.core.config import settings

    root = Path(settings.archive_dir) / "products"
    return {p.name: p / "attachments" for p in root.iterdir()} if root.is_dir() else {}


@pytest.mark.asyncio
async def test_the_export_carries_every_member_exactly_once(exported):
    data, manifest, _ = exported
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names)), names
        assert sorted(names) == sorted(
            [
                "product.json",
                *[f["member"] for f in manifest["files"]],
                *[a["member"] for a in manifest["attachments"]],
            ]
        )
        # Two files, deduped by hash, and the bytes really are the file's own.
        assert len(manifest["files"]) == 2
        for entry in manifest["files"]:
            assert hashlib.sha256(zf.read(entry["member"])).hexdigest() == entry["hash"]

    assert manifest["format"] == 1 and manifest["exported_at"].endswith("+00:00")
    assert manifest["card"]["name"] == "Desk Lamp"
    assert manifest["card"]["designer"] == "Chef&koch" and manifest["card"]["design_id"] == "1234567"
    assert {p["name_key"]: p["qty_per_unit"] for p in manifest["parts"]} == {
        "hook.stl": 4,
        "clip.stl": 1,
        "shade.stl": 1,
        "purchased:m3 screw": 8,
    }
    assert [a["category"] for a in manifest["attachments"]] == ["pictures", "bom_docs", "assembly"]
    assert manifest["cover"] == "shot.png"
    assert {(p["filename"], p["plate_index"]) for p in manifest["plates"]} == {
        ("lamp.gcode.3mf", 1),
        ("lamp.gcode.3mf", 2),
        ("shade.gcode.3mf", 0),
    }


@pytest.mark.asyncio
async def test_the_export_filename_is_a_slug_and_a_date(committing_client):
    pid = (await committing_client.post("/api/v1/products/", json={"name": "Desk Lamp / Mk II"})).json()["id"]
    r = await committing_client.get(f"/api/v1/products/{pid}/export")
    assert r.status_code == 200
    assert f'filename="desk-lamp-mk-ii_{_today()}.zip"' in r.headers["content-disposition"]

    # A name with nothing ASCII-alphanumeric in it: the slug fills the legacy
    # parameter and the real name still reaches the browser through
    # ``filename*``. Stripping the non-ASCII out of it names the file after the
    # date and nothing else, which is the bug the fallback exists to avoid.
    name = "Настільна лампа"
    pid = (await committing_client.post("/api/v1/products/", json={"name": name})).json()["id"]
    r = await committing_client.get(f"/api/v1/products/{pid}/export")
    assert r.headers["content-disposition"] == build_content_disposition(
        f"{name}_{_today()}.zip", ascii_fallback=f"product-{pid}_{_today()}.zip"
    )


@pytest.mark.asyncio
async def test_the_export_streams_from_a_temp_file_that_is_then_removed(committing_client, exported):
    """The archive is never held in memory, and it does not outlive the response."""
    data, _, _ = exported
    body = (await _import(committing_client, data)).json()
    before = _temp_exports()
    r = await committing_client.get(f"/api/v1/products/{body['product']['id']}/export")
    assert r.status_code == 200 and len(r.content) > 0
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
    assert len(names) == len(set(names)), names
    assert len([n for n in names if n.startswith("files/")]) == 2, names
    assert _temp_exports() == before, "the export left its temp file behind"


@pytest.mark.asyncio
async def test_the_round_trip_rebuilds_the_product_and_respects_the_library(committing_client, db_session, exported):
    data, manifest, ids = exported
    before = await _active_files(db_session)

    r = await _import(committing_client, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warnings"] == []
    product = body["product"]

    # The card came back whole.
    assert product["name"] == "Desk Lamp" and product["designer"] == "Chef&koch"
    assert product["description"] == "A lamp & a shade" and product["design_id"] == "1234567"
    assert product["source_url"] == "https://makerworld.com/models/1234567"

    # Parts: same keys, same quantities, same aliases, same purchase data.
    assert {p["name_key"]: p["qty_per_unit"] for p in product["parts"]} == {
        "hook.stl": 4,
        "clip.stl": 1,
        "shade.stl": 1,
        "purchased:m3 screw": 8,
    }
    purchased = next(p for p in product["parts"] if p["kind"] == "purchased")
    assert purchased["unit_price"] == 0.05
    assert next(p for p in product["parts"] if p["name_key"] == "hook.stl")["aliases"] == ["hook.stl"]

    # The surviving file was matched by hash — no second row for it. The trashed
    # one had to be ingested again, so exactly ONE row is new.
    assert await _active_files(db_session) == before + 1
    assert ids["shared_file_id"] in product["library_file_ids"]
    assert len(product["library_file_ids"]) == 2

    # Plates are the sync's, derived from the files themselves.
    plates = (await committing_client.get(f"/api/v1/products/{product['id']}/plates")).json()
    assert sorted((p["filename"], p["plate_index"]) for p in plates) == [
        ("lamp.gcode.3mf", 1),
        ("lamp.gcode.3mf", 2),
        ("shade.gcode.3mf", 0),
    ]

    # Attachments and the cover, with the bytes on disk.
    attachments = {a["original_name"]: a for a in product["attachments"]}
    assert sorted(attachments) == ["bom.csv", "guide.pdf", "shot.png"]
    assert attachments["shot.png"]["category"] == "pictures"
    assert attachments["shot.png"]["source"] == "import"
    assert attachments["shot.png"]["source_file_id"] is None
    directory = product_attachments_dir(product["id"])
    assert (directory / attachments["bom.csv"]["filename"]).read_bytes() == BOM
    assert product["cover_image_filename"] == attachments["shot.png"]["filename"]
    assert product["has_cover"] is True


@pytest.mark.asyncio
async def test_an_import_joins_the_products_a_reused_file_already_has(committing_client, db_session, exported):
    """A file matched by hash keeps every product it already belongs to."""
    data, _, ids = exported
    keeper = (await committing_client.post("/api/v1/products/", json={"name": "Keeper"})).json()["id"]
    assert (
        await committing_client.put(
            f"/api/v1/products/{keeper}/files", json={"library_file_ids": [ids["shared_file_id"]]}
        )
    ).status_code == 200

    imported = (await _import(committing_client, data)).json()["product"]
    assert ids["shared_file_id"] in imported["library_file_ids"]
    assert (await committing_client.get(f"/api/v1/products/{keeper}")).json()["library_file_ids"] == [
        ids["shared_file_id"]
    ]
    kept = (await db_session.execute(select(ProductPlate).where(ProductPlate.product_id == keeper))).scalars().all()
    assert kept, "the co-owner lost its plates when the import synced the shared file"


@pytest.mark.asyncio
async def test_the_import_folder_is_the_operators_when_they_name_one(committing_client, db_session, exported):
    data, _, _ = exported
    folder = (await committing_client.post("/api/v1/library/folders", json={"name": "Imports"})).json()
    body = (await _import(committing_client, data, folder_id=str(folder["id"]))).json()
    landed = (
        (await db_session.execute(select(LibraryFile.id).where(LibraryFile.folder_id == folder["id"]))).scalars().all()
    )
    assert landed, "the re-ingested file did not land in the folder the operator named"
    assert set(landed) <= set(body["product"]["library_file_ids"])


@pytest.mark.asyncio
async def test_without_a_folder_the_import_makes_one_named_after_the_product(committing_client, db_session, exported):
    data, _, _ = exported
    body = (await _import(committing_client, data)).json()
    made = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "Desk Lamp"))).scalars().all()
    assert len(made) == 1, "the import invented no destination folder"
    landed = (
        (await db_session.execute(select(LibraryFile.id).where(LibraryFile.folder_id == made[0].id))).scalars().all()
    )
    assert set(landed) <= set(body["product"]["library_file_ids"])
    # A destination, not a link: the folder must not join the product, or every
    # file dropped in it later would silently become part of the recipe.
    assert body["product"]["library_folder_ids"] == []

    # Importing the same export again — how an operator retries — must not leave
    # a second "Desk Lamp" folder beside the first.
    await _import(committing_client, data)
    again = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "Desk Lamp"))).scalars().all()
    assert len(again) == 1, "a second import invented a second folder of the same name"


@pytest.mark.asyncio
async def test_a_manifest_plate_the_file_no_longer_carries_is_one_warning(committing_client, exported):
    data, manifest, _ = exported
    manifest["plates"].append({**manifest["plates"][0], "plate_index": 7})
    manifest["plates"].append({**manifest["plates"][0], "plate_index": "nonsense"})
    r = await _import(committing_client, _rebuild(data, manifest=manifest))
    assert r.status_code == 200, r.text
    warnings = r.json()["warnings"]
    assert _codes(warnings) == ["import_plate_missing", "import_plate_missing"], warnings
    # An index that is not a number cannot be checked against anything, so it is
    # reported rather than silently dropped.
    assert [w["plate_index"] for w in _params(warnings, "import_plate_missing")] == [7, "nonsense"]
    assert {w["filename"] for w in _params(warnings, "import_plate_missing")} == {"shade.gcode.3mf"}


@pytest.mark.asyncio
async def test_a_member_that_climbs_out_of_the_archive_is_refused(committing_client, exported):
    data, _, _ = exported
    for evil in ("../evil", "files/../../evil", "/etc/passwd", "C:/evil.txt", "notes/loose.txt"):
        r = await _import(committing_client, _rebuild(data, extra={evil: b"x"}))
        assert r.status_code == 400, f"{evil} was accepted: {r.status_code}"


@pytest.mark.asyncio
async def test_a_format_this_bamdude_does_not_read_is_refused(committing_client, exported):
    data, manifest, _ = exported
    assert (await _import(committing_client, _rebuild(data, manifest={**manifest, "format": 2}))).status_code == 400
    assert (await _import(committing_client, _rebuild(data, manifest={"format": 1}))).status_code == 400
    assert (await _import(committing_client, b"not a zip at all")).status_code == 400


@pytest.mark.asyncio
async def test_nothing_is_written_when_the_manifest_is_refused(committing_client, db_session, exported):
    """The validation runs before the first write, so a rejected archive leaves
    neither a product nor a library row behind."""
    data, manifest, _ = exported
    products = len((await committing_client.get("/api/v1/products/")).json())
    files = await _active_files(db_session)
    assert (await _import(committing_client, _rebuild(data, manifest={**manifest, "format": 99}))).status_code == 400
    assert len((await committing_client.get("/api/v1/products/")).json()) == products
    assert await _active_files(db_session) == files


@pytest.mark.asyncio
async def test_an_attachment_its_category_does_not_carry_is_skipped_with_a_warning(committing_client, exported):
    """The category allowlists are the only defence against an executable
    landing in the attachments directory — an import must not be the way in."""
    data, manifest, _ = exported
    manifest["attachments"].append(
        {
            "category": "pictures",
            "original_name": "run.exe",
            "sort_order": 9,
            "source": "manual",
            "member": "attachments/pictures/run.exe",
        }
    )
    r = await _import(
        committing_client, _rebuild(data, manifest=manifest, extra={"attachments/pictures/run.exe": b"MZ"})
    )
    assert r.status_code == 200, r.text
    product = r.json()["product"]
    assert _params(r.json()["warnings"], "skipped_extension") == [
        {"name": "run.exe", "ext": ".exe", "category": "pictures"}
    ], r.json()["warnings"]
    assert "run.exe" not in [a["original_name"] for a in product["attachments"]]
    assert not list(product_attachments_dir(product["id"]).glob("*.exe"))


@pytest.mark.asyncio
async def test_a_dedicated_cover_survives_the_round_trip(committing_client):
    """A cover uploaded straight to the column is not a gallery entry — it still
    has to come back, and it still must not join the gallery."""
    pid = (await committing_client.post("/api/v1/products/", json={"name": "Bare"})).json()["id"]
    assert (
        await committing_client.put(
            f"/api/v1/products/{pid}/cover-image", files={"file": ("dedicated.png", COVER, "image/png")}
        )
    ).status_code == 200

    data = (await committing_client.get(f"/api/v1/products/{pid}/export")).content
    # The column keeps only ``cover_<uuid>.png`` — an uploaded cover never had
    # an original name to keep, so the export gives it a stable readable one.
    assert _manifest(data)["cover"] == "cover.png"
    body = (await _import(committing_client, data)).json()
    product = body["product"]
    assert product["attachments"] == []
    stored = product["cover_image_filename"]
    assert stored and stored.startswith("cover_")
    assert (product_attachments_dir(product["id"]) / stored).read_bytes() == COVER


@pytest.mark.asyncio
async def test_a_declared_length_over_the_ceiling_is_refused_before_anything_is_read(
    committing_client, db_session, monkeypatch, exported
):
    data, _, _ = exported
    monkeypatch.setattr("backend.app.services.product_files.MAX_IMPORT_BYTES", 8)
    products = len((await committing_client.get("/api/v1/products/")).json())
    files = await _active_files(db_session)
    r = await _import(committing_client, data)
    assert r.status_code == 413, r.text
    assert len((await committing_client.get("/api/v1/products/")).json()) == products
    assert await _active_files(db_session) == files


@pytest.mark.asyncio
async def test_a_body_over_the_ceiling_is_refused_when_no_length_was_declared(
    committing_client, db_session, monkeypatch, exported
):
    """The declared length is the client's word and can simply be absent. The
    size of the part the parser already spooled is the fact, and it is the gate
    that counts."""
    data, _, _ = exported
    monkeypatch.setattr("backend.app.services.product_files.MAX_IMPORT_BYTES", 64)
    products = len((await committing_client.get("/api/v1/products/")).json())
    files = await _active_files(db_session)
    r = await _import_chunked(committing_client, data)
    assert r.status_code == 413, r.text
    assert len((await committing_client.get("/api/v1/products/")).json()) == products
    assert await _active_files(db_session) == files


@pytest.mark.asyncio
async def test_a_library_member_over_the_per_member_cap_is_skipped(committing_client, monkeypatch, exported):
    """``store_library_upload`` takes bytes, so a member is materialised whole —
    the cap is a memory bound, and crossing it costs that file and nothing else."""
    data, manifest, ids = exported
    # Below the SMALLEST member, so both are refused and the product comes back
    # with nothing bound to it rather than half a recipe.
    smallest = min(f["size"] for f in manifest["files"])
    monkeypatch.setattr("backend.app.services.product_files.MAX_IMPORT_MEMBER_BYTES", smallest - 1)
    r = await _import(committing_client, data)
    assert r.status_code == 200, r.text
    skipped = _params(r.json()["warnings"], "skipped_too_large")
    assert [w["category"] for w in skipped] == ["files", "files"], r.json()["warnings"]
    assert {w["size"] for w in skipped} == {f["size"] for f in manifest["files"]}
    # The product is still there, and it simply has no files bound to it.
    assert r.json()["product"]["library_file_ids"] == []
    assert r.json()["product"]["name"] == "Desk Lamp"


@pytest.mark.asyncio
async def test_an_external_folder_of_the_same_name_never_receives_the_import(
    committing_client, db_session, tmp_path, exported
):
    """An external root folder is a window onto somebody's mount, and the upload
    path WRITES THROUGH to it. Reusing one because the names match would put the
    operator's 3MFs on a share they never named — or, read-only, 403 out of the
    ingest loop and kill the whole import (403 is not 400, so it does not
    degrade)."""
    data, _, _ = exported
    mount = tmp_path / "mount"
    mount.mkdir()
    r = await committing_client.post(
        "/api/v1/library/folders/external", json={"name": "Desk Lamp", "external_path": str(mount)}
    )
    assert r.status_code == 200, r.text
    external_id = r.json()["id"]

    body = (await _import(committing_client, data)).json()
    assert body["warnings"] == [], body["warnings"]
    folders = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "Desk Lamp"))).scalars().all()
    made = [f for f in folders if not f.is_external]
    assert len(made) == 1, "the import did not make its own managed folder"
    landed = (
        (await db_session.execute(select(LibraryFile.id).where(LibraryFile.folder_id == made[0].id))).scalars().all()
    )
    assert set(landed) <= set(body["product"]["library_file_ids"]) and landed
    assert list(mount.iterdir()) == [], "the import wrote onto an external mount it was never pointed at"
    assert (
        await db_session.execute(select(LibraryFile.id).where(LibraryFile.folder_id == external_id))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_an_attachment_member_that_cannot_be_read_leaves_no_orphan(committing_client, exported):
    """``zf.read`` raises on a bad CRC. Every file written before that point has
    to come back off the disk, or the product page names pictures that are there
    and the directory holds pictures nothing names."""
    data, manifest, _ = exported
    guide = next(a for a in manifest["attachments"] if a["original_name"] == "guide.pdf")
    broken = _corrupt_member(data, guide["member"])

    # Not first, or nothing has been written by the time the read blows up and
    # this test proves nothing.
    assert manifest["attachments"].index(guide) > 0

    # The archive still OPENS and still validates — a bad CRC is only found when
    # the member is read. Asserting the exact exception matters: a 400 from the
    # manifest check is an ``Exception`` too, and it would mean the import never
    # got as far as writing anything.
    before = _attachment_files()
    with pytest.raises(zipfile.BadZipFile):
        await _import(committing_client, broken)
    assert _attachment_files() == before, "the failed import left attachment files nothing will ever reference"


@pytest.mark.asyncio
async def test_a_file_the_library_refuses_costs_that_file_and_nothing_else(committing_client, exported):
    """A 400 from the library is about ONE member. The product is still built —
    an operator with one unprintable part and a warning on screen is better off
    than one with a 400 and nothing."""
    data, manifest, _ = exported
    manifest["files"].append({"hash": "0" * 64, "filename": "broken.3mf", "size": 9, "member": "files/broken.3mf"})
    r = await _import(committing_client, _rebuild(data, manifest=manifest, extra={"files/broken.3mf": b"not a zip"}))
    assert r.status_code == 200, r.text
    refused = _params(r.json()["warnings"], "import_file_refused")
    assert len(refused) == 1 and refused[0]["name"] == "broken.3mf"
    # The library's own words, passed through rather than re-invented here.
    assert "3mf" in refused[0]["detail"].lower()
    assert len(r.json()["product"]["library_file_ids"]) == 2
    assert r.json()["product"]["name"] == "Desk Lamp"


@pytest.mark.asyncio
async def test_a_part_that_is_deliberately_not_counted_round_trips_as_zero(committing_client):
    """``qty_per_unit = 0`` is the model's "on a plate but not part of the
    product" rule. A round trip that raised it to 1 would invent a requirement
    the operator deliberately removed."""
    pid = (await committing_client.post("/api/v1/products/", json={"name": "Zeroed"})).json()["id"]
    r = await committing_client.post(
        f"/api/v1/products/{pid}/parts", json={"kind": "purchased", "name": "Spacer", "qty_per_unit": 0}
    )
    assert r.status_code == 200, r.text
    data = (await committing_client.get(f"/api/v1/products/{pid}/export")).content
    imported = (await _import(committing_client, data)).json()["product"]
    assert [(p["name"], p["qty_per_unit"]) for p in imported["parts"]] == [("Spacer", 0)]


@pytest.mark.asyncio
async def test_export_of_a_product_that_is_not_there_is_a_404(committing_client):
    assert (await committing_client.get("/api/v1/products/999999/export")).status_code == 404


@pytest.mark.asyncio
async def test_import_into_a_folder_that_is_not_there_is_a_404(committing_client, exported):
    data, _, _ = exported
    assert (await _import(committing_client, data, folder_id="999999")).status_code == 404


@pytest.mark.asyncio
async def test_a_product_with_nothing_on_it_still_exports_and_imports(committing_client):
    pid = (await committing_client.post("/api/v1/products/", json={"name": "Empty"})).json()["id"]
    data = (await committing_client.get(f"/api/v1/products/{pid}/export")).content
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["product.json"]
    body = (await _import(committing_client, data)).json()
    assert body["product"]["name"] == "Empty" and body["warnings"] == []
    assert body["product"]["id"] != pid


@pytest.mark.asyncio
async def test_a_file_whose_bytes_are_gone_is_left_out_of_the_export(committing_client, db_session):
    """A row can outlive its bytes. The export skips it rather than failing."""
    pid = (await committing_client.post("/api/v1/products/", json={"name": "Ghost"})).json()["id"]
    row = LibraryFile(filename="gone.3mf", file_path="library/gone.3mf", file_size=1, file_type="3mf")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/files", json={"library_file_ids": [row.id]})
    ).status_code == 200

    data = (await committing_client.get(f"/api/v1/products/{pid}/export")).content
    assert _manifest(data)["files"] == []
