"""The printer file manager's plates come from the archive when the file has been
printed (no FTP), and from ONE read otherwise, thumbnails inside the JSON (spec §3.2-§3.4).

Five answers, one question each:

* a printed file — the archive's own 3MF is on disk, so nothing is fetched and
  every ``thumbnail_url`` points at the anonymous archive routes an ``<img>``
  can load;
* a retention-*cleaned* archive that still carries ``extra_data["plates"]`` —
  the printed plate's extracted PNG answers, its siblings cannot (their picture
  lived in the deleted 3MF);
* a cleaned archive with no cached plates — the archive has nothing to offer and
  the route falls through exactly as if no archive existed;
* a never-printed file — read once, PNGs ride in the JSON as data URLs, and the
  second request is served from the cache;
* the token-less per-plate image route is gone.
"""

import base64
import io
import zipfile

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
    committing_client, db_session, transport, tmp_path
):
    """Nothing to offer: no 3MF to open and no cached plates, so the printer is read."""
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

    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/Bare.3mf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["archive_id"] is None
    assert [pl["index"] for pl in body["plates"]] == [1, 2]
    assert all(pl["thumbnail_url"].startswith("data:image/png;base64,") for pl in body["plates"])
    assert transport.reads == 1


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


async def test_a_non_3mf_path_answers_empty_without_a_read(committing_client, db_session, transport):
    p = await _printer(db_session)
    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plates?path=/cache/readme.txt")
    assert r.status_code == 200 and r.json()["plates"] == [] and transport.reads == 0


async def test_the_token_less_thumbnail_route_is_gone(committing_client, db_session, transport):
    p = await _printer(db_session)
    r = await committing_client.get(f"/api/v1/printers/{p.id}/files/plate-thumbnail/1?path=/cache/new.3mf")
    assert r.status_code in (404, 405)
