"""The printer file manager's plates come from the archive when the file has been
printed (no FTP), and from ONE read otherwise, thumbnails inside the JSON (spec §3.2-§3.4).

One question each:

* a printed file — the archive's own 3MF is on disk, so nothing is fetched and
  every ``thumbnail_url`` points at the anonymous archive routes an ``<img>``
  can load;
* a retention-*cleaned* archive that still carries ``extra_data["plates"]`` —
  the printed plate's extracted PNG answers, its siblings cannot (their picture
  lived in the deleted 3MF);
* a cleaned archive with no cached plates — the archive has nothing to offer and
  the route falls through exactly as if no archive existed;
* a row whose 3MF vanished while ``file_path`` still names it (pruned or moved,
  not retention) — the printed plate's surviving PNG answers and its siblings get
  ``null``, never a URL that would 404;
* the printed plate's extracted PNG answers even when the container never had a
  ``Metadata/plate_N.png`` — two different facts, asked in the right order;
* a never-printed file — read once, PNGs ride in the JSON as data URLs, and the
  second request is served from the cache, which expires and evicts as specified;
* a torn 3MF — on the card or in the archive — degrades instead of failing;
* the token-less per-plate image route is gone.
"""

import base64
import io
import zipfile
import zlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.api.routes import printers as printers_route
from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _three_mf(plates: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in range(1, plates + 1):
            zf.writestr(f"Metadata/plate_{n}.png", PNG)
            zf.writestr(f"Metadata/plate_{n}.gcode", "; gcode")
        zf.writestr(
            "Metadata/slice_info.config",
            "<config>"
            + "".join(f'<plate><metadata key="index" value="{n}"/></plate>' for n in range(1, plates + 1))
            + "</config>",
        )
    return buf.getvalue()


def _three_mf_with_a_corrupt_member(member: str = "Metadata/slice_info.config") -> bytes:
    """A zip whose deflated *member* decompresses to a ``zlib.error``.

    A 3MF half-written to the card raises ``zipfile.BadZipFile`` most of the time
    (the CRC fails) and ``zlib.error`` the rest of it ("invalid block type") — and
    only the second kind escapes a guard that lists BadZipFile alone, so this
    walks single-byte mangles of the compressed stream until it gets one. Each
    mangle keeps the file's length, so the central directory stays valid and the
    reader gets as far as decompressing the member.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/plate_1.png", PNG)
        zf.writestr("Metadata/plate_1.gcode", "; gcode\n" * 200)
        zf.writestr(
            "Metadata/slice_info.config",
            "<config><!--" + "pad " * 200 + '--><plate><metadata key="index" value="1"/></plate></config>',
        )
    good = buf.getvalue()
    info = zipfile.ZipFile(io.BytesIO(good)).getinfo(member)
    head = info.header_offset
    name_len = int.from_bytes(good[head + 26 : head + 28], "little")
    extra_len = int.from_bytes(good[head + 28 : head + 30], "little")
    data_start = head + 30 + name_len + extra_len
    for offset in range(info.compress_size):
        raw = bytearray(good)
        raw[data_start + offset] ^= 0xFF
        payload = bytes(raw)
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                zf.read(member)
        except zlib.error:
            return payload
        except Exception:
            continue
    raise AssertionError(f"no single-byte mangle of {member} decompressed to a zlib.error")


def _cached_plates(count: int) -> list[dict]:
    """What ``parse_plates_from_3mf`` left in ``extra_data["plates"]`` at archive time."""
    return [
        {
            "index": n,
            "name": f"plate {n}",
            "objects": [],
            "object_count": 0,
            "has_thumbnail": True,
            "print_time_seconds": None,
            "filament_used_grams": None,
            "filaments": [],
        }
        for n in range(1, count + 1)
    ]


class _Transport:
    def __init__(self, data: bytes | None):
        self.data, self.reads = data, 0

    async def read_bytes(self, path: str):
        self.reads += 1
        return self.data


@pytest.fixture
def transport(monkeypatch):
    t = _Transport(_three_mf(2))
    monkeypatch.setattr(printers_route, "transport_for", lambda printer, storage: t)
    monkeypatch.setattr(printers_route, "_resolve_storage", lambda storage, model, status: "internal")
    monkeypatch.setattr(printers_route, "_plates_cache", {})
    return t


@pytest.fixture
def clock(monkeypatch):
    """The clock the plates cache reads, under the test's control.

    Only the two cache helpers use ``time`` in the route module, so swapping the
    whole name is safe and leaves the real clock alone everywhere else.
    """
    now = [1_000.0]
    monkeypatch.setattr(printers_route, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


async def _printer(db) -> Printer:
    p = Printer(name="p", ip_address="10.0.0.1", access_code="00000000", serial_number="PLATES1", model="X1C")
    db.add(p)
    await db.commit()
    return p


async def _archive_id(db) -> int:
    return (await db.execute(select(PrintArchive.id))).scalar()


async def test_a_printed_file_answers_from_the_archive_without_touching_the_printer(
    committing_client, db_session, transport, tmp_path
):
    p = await _printer(db_session)
    assert settings.base_dir == tmp_path  # the autouse data_dir_isolation fixture
    (tmp_path / "archive/1").mkdir(parents=True)
    (tmp_path / "archive/1/My Model.gcode.3mf").write_bytes(_three_mf(2))
    (tmp_path / "archive/1/thumb.png").write_bytes(PNG)
    db_session.add(
        PrintArchive(
            printer_id=p.id,
            filename="My Model.gcode.3mf",
            file_path="archive/1/My Model.gcode.3mf",
            file_size=1,
            print_name="My Model",
            source_content_hash="a" * 64,
            thumbnail_path="archive/1/thumb.png",
            plate_index=2,
        )
    )
    await db_session.commit()
    archive_id = await _archive_id(db_session)

    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/My_Model.3mf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archive_id"] == archive_id
    urls = {pl["index"]: pl["thumbnail_url"] for pl in body["plates"]}
    assert urls == {
        1: f"/api/v1/archives/{archive_id}/plate-thumbnail/1",
        2: f"/api/v1/archives/{archive_id}/thumbnail",  # the printed plate: its PNG is already on disk
    }
    assert body["is_multi_plate"] is True
    assert all(pl["has_thumbnail"] for pl in body["plates"])
    assert transport.reads == 0


async def test_a_retention_cleaned_archive_answers_from_its_cached_plates(
    committing_client, db_session, transport, tmp_path
):
    """3MF deleted, picture kept: the printed plate answers, its siblings cannot."""
    p = await _printer(db_session)
    (tmp_path / "archive/1").mkdir(parents=True)
    (tmp_path / "archive/1/thumb.png").write_bytes(PNG)
    db_session.add(
        PrintArchive(
            printer_id=p.id,
            filename="Cleaned.gcode.3mf",
            file_path="",
            file_size=1,
            print_name="Cleaned",
            source_content_hash="b" * 64,
            thumbnail_path="archive/1/thumb.png",
            plate_index=2,
            extra_data={"plates": _cached_plates(2)},
        )
    )
    await db_session.commit()
    archive_id = await _archive_id(db_session)

    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/Cleaned.3mf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archive_id"] == archive_id
    urls = {pl["index"]: pl["thumbnail_url"] for pl in body["plates"]}
    assert urls == {1: None, 2: f"/api/v1/archives/{archive_id}/thumbnail"}
    # ``has_thumbnail`` follows the URL: plate 1's picture died with the 3MF, so
    # a caller keying its placeholder off that flag never renders a null <img>.
    assert {pl["index"]: pl["has_thumbnail"] for pl in body["plates"]} == {1: False, 2: True}
    assert transport.reads == 0


async def test_a_retention_cleaned_archive_without_cached_plates_falls_through_to_one_read(
    committing_client, db_session, transport, tmp_path, monkeypatch
):
    """Nothing to offer: no 3MF to open and no cached plates, so the printer is read.

    The answer alone cannot tell this apart from "there was no archive", so the
    resolver is spied on: the row must be FOUND and then declined, or this test
    would keep passing if the resolver stopped returning cleaned rows at all.
    """
    resolved: list[tuple | None] = []
    real_resolver = printers_route.find_archive_for_sd_file

    async def spy(db, printer_id, sd_name):
        row = await real_resolver(db, printer_id, sd_name)
        resolved.append(None if row is None else (row.id, row.file_path, row.thumbnail_path))
        return row

    monkeypatch.setattr(printers_route, "find_archive_for_sd_file", spy)

    p = await _printer(db_session)
    (tmp_path / "archive/1").mkdir(parents=True)
    (tmp_path / "archive/1/thumb.png").write_bytes(PNG)
    db_session.add(
        PrintArchive(
            printer_id=p.id,
            filename="Bare.gcode.3mf",
            file_path="",
            file_size=1,
            print_name="Bare",
            source_content_hash="c" * 64,
            thumbnail_path="archive/1/thumb.png",
            plate_index=1,
        )
    )
    await db_session.commit()
    archive_id = await _archive_id(db_session)

    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/Bare.3mf")
    assert r.status_code == 200, r.text
    # The resolver DID hand over the cleaned row — this is a decline, not a miss.
    assert resolved == [(archive_id, "", "archive/1/thumb.png")]
    body = r.json()
    assert body["archive_id"] is None
    assert [pl["index"] for pl in body["plates"]] == [1, 2]
    assert all(pl["thumbnail_url"].startswith("data:image/png;base64,") for pl in body["plates"])
    assert transport.reads == 1


async def test_a_row_whose_3mf_vanished_offers_only_the_printed_plates_png(
    committing_client, db_session, transport, tmp_path
):
    """``file_path`` still names a 3MF that is no longer there.

    Not retention — retention blanks the column. A prune, a move or a restore
    that missed the archive directory leaves the path set and the file gone, and
    a sibling plate's ``/archives/{id}/plate-thumbnail/{n}`` would open exactly
    that missing file. So the siblings answer ``null`` and the modal draws its
    placeholder, while the printed plate's already-extracted PNG still answers.
    """
    p = await _printer(db_session)
    (tmp_path / "archive/1").mkdir(parents=True)
    (tmp_path / "archive/1/thumb.png").write_bytes(PNG)  # the 3MF is deliberately NOT written
    db_session.add(
        PrintArchive(
            printer_id=p.id,
            filename="Vanished.gcode.3mf",
            file_path="archive/1/Vanished.gcode.3mf",
            file_size=1,
            print_name="Vanished",
            source_content_hash="f" * 64,
            thumbnail_path="archive/1/thumb.png",
            plate_index=2,
            extra_data={"plates": _cached_plates(2)},
        )
    )
    await db_session.commit()
    archive_id = await _archive_id(db_session)

    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/Vanished.3mf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archive_id"] == archive_id
    urls = {pl["index"]: pl["thumbnail_url"] for pl in body["plates"]}
    assert urls == {1: None, 2: f"/api/v1/archives/{archive_id}/thumbnail"}
    assert {pl["index"]: pl["has_thumbnail"] for pl in body["plates"]} == {1: False, 2: True}
    assert transport.reads == 0


async def test_a_never_printed_file_is_read_once_and_carries_its_pictures(committing_client, db_session, transport):
    p = await _printer(db_session)
    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/new.3mf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archive_id"] is None
    assert [pl["index"] for pl in body["plates"]] == [1, 2]
    assert all(pl["thumbnail_url"].startswith("data:image/png;base64,") for pl in body["plates"])
    assert base64.b64decode(body["plates"][0]["thumbnail_url"].split(",", 1)[1]) == PNG
    assert transport.reads == 1

    again = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/new.3mf")
    assert again.json() == body
    assert transport.reads == 1  # served from the cache


async def test_the_printed_plates_own_png_answers_even_when_the_container_has_no_plate_png(
    committing_client, db_session, transport, tmp_path
):
    """The extracted PNG is on disk whatever the plate metadata says about it.

    ``thumbnail_path`` is a fact about the archive directory; ``has_thumbnail`` is
    a fact about the 3MF's ``Metadata/plate_N.png``. A thumbnail extracted or
    generated another way makes the second False while the first is still a real
    picture, so the printed-plate rule is asked BEFORE the metadata gate.
    """
    p = await _printer(db_session)
    (tmp_path / "archive/1").mkdir(parents=True)
    (tmp_path / "archive/1/Model.gcode.3mf").write_bytes(_three_mf(2))
    (tmp_path / "archive/1/thumb.png").write_bytes(PNG)
    plates = _cached_plates(2)
    plates[1]["has_thumbnail"] = False  # plate 2 — the one that printed
    db_session.add(
        PrintArchive(
            printer_id=p.id,
            filename="Model.gcode.3mf",
            file_path="archive/1/Model.gcode.3mf",
            file_size=1,
            print_name="Model",
            source_content_hash="e" * 64,
            thumbnail_path="archive/1/thumb.png",
            plate_index=2,
            extra_data={"plates": plates},
        )
    )
    await db_session.commit()
    archive_id = await _archive_id(db_session)

    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/Model.3mf")
    assert r.status_code == 200, r.text
    body = r.json()
    urls = {pl["index"]: pl["thumbnail_url"] for pl in body["plates"]}
    assert urls == {
        1: f"/api/v1/archives/{archive_id}/plate-thumbnail/1",
        2: f"/api/v1/archives/{archive_id}/thumbnail",
    }
    assert {pl["index"]: pl["has_thumbnail"] for pl in body["plates"]} == {1: True, 2: True}
    assert transport.reads == 0


async def test_a_torn_3mf_on_the_card_answers_empty_instead_of_failing(committing_client, db_session, transport):
    """A mangled deflate stream is a ``zlib.error``, which is not a ``BadZipFile``."""
    payload = _three_mf_with_a_corrupt_member()
    with pytest.raises(zlib.error), zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.read("Metadata/slice_info.config")
    p = await _printer(db_session)
    transport.data = payload

    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/torn.3mf")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "printer_id": p.id,
        "path": "/cache/torn.3mf",
        "filename": "torn.3mf",
        "plates": [],
        "is_multi_plate": False,
        "archive_id": None,
    }
    assert transport.reads == 1


async def test_a_torn_archive_on_disk_falls_through_to_the_printer(committing_client, db_session, transport, tmp_path):
    """The archive is unreadable; the printer may still hold the file."""
    p = await _printer(db_session)
    (tmp_path / "archive/1").mkdir(parents=True)
    (tmp_path / "archive/1/Torn.gcode.3mf").write_bytes(_three_mf_with_a_corrupt_member())
    db_session.add(
        PrintArchive(
            printer_id=p.id,
            filename="Torn.gcode.3mf",
            file_path="archive/1/Torn.gcode.3mf",
            file_size=1,
            print_name="Torn",
            source_content_hash="d" * 64,
            plate_index=1,
        )
    )
    await db_session.commit()

    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/Torn.3mf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archive_id"] is None
    assert [pl["index"] for pl in body["plates"]] == [1, 2]
    assert transport.reads == 1


async def test_the_cached_answer_is_reread_after_the_ttl(committing_client, db_session, transport, clock):
    p = await _printer(db_session)
    url = f"/api/v1/printers/{p.id}/files/plates?path=/cache/new.3mf"
    assert (await committing_client.get(url)).status_code == 200
    assert transport.reads == 1

    clock[0] += printers_route._PLATES_CACHE_TTL - 1  # still inside the window
    assert (await committing_client.get(url)).status_code == 200
    assert transport.reads == 1

    clock[0] += 2  # past it
    assert (await committing_client.get(url)).status_code == 200
    assert transport.reads == 2


def test_the_cache_holds_thirty_two_entries_and_drops_the_oldest(transport, clock):
    for n in range(printers_route._PLATES_CACHE_MAX + 1):
        clock[0] += 1
        printers_route._plates_cache_put((1, "internal", f"/cache/{n}.3mf"), {"plates": []})

    assert printers_route._PLATES_CACHE_MAX == 32
    assert len(printers_route._plates_cache) == 32
    assert printers_route._plates_cache_get((1, "internal", "/cache/0.3mf")) is None  # evicted
    assert printers_route._plates_cache_get((1, "internal", "/cache/1.3mf")) is not None
    assert printers_route._plates_cache_get((1, "internal", "/cache/32.3mf")) is not None


async def test_a_non_3mf_path_answers_empty_without_a_read(committing_client, db_session, transport):
    p = await _printer(db_session)
    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/readme.txt")
    assert r.status_code == 200 and r.json()["plates"] == [] and transport.reads == 0


async def test_the_token_less_thumbnail_route_is_gone(committing_client, db_session, transport):
    """Absent from the schema, not merely unreachable.

    A 404 is also what a typo'd path, a renamed prefix or a wrong method answers,
    so the discriminating assertion is the OpenAPI one. The sibling
    ``/files/plates`` path is asserted present alongside it: that pins the key
    form the schema actually uses, so "not in paths" cannot pass by misspelling.
    """
    from backend.app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/printers/{printer_id}/files/plates" in paths
    assert "/api/v1/printers/{printer_id}/files/plate-thumbnail/{plate_index}" not in paths

    p = await _printer(db_session)
    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plate-thumbnail/1?path=/cache/new.3mf")
    assert r.status_code in (404, 405)
