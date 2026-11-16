"""Saving labels as pictures: one PNG for one label, a ZIP for several.

The door takes what the label print takes — the spools, the design, the paper —
plus a resolution. The picture is the design itself at its own size: a design
printed on a sheet is saved label by label, never as the page.
"""

from __future__ import annotations

import io
import struct
import threading
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import text

from backend.app.core.auth import get_password_hash
from backend.app.models.group import Group
from backend.app.models.label_template import LabelSheet, LabelTemplate
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.user import User
from backend.app.services import label_picture
from backend.app.services.label_seed import BUILTIN_SHEETS
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

LOCAL = "/api/v1/inventory/labels/png"
SPOOLMAN = "/api/v1/spoolman/labels/png"

IVORY = (0xF5, 0xE6, 0xD3)


@pytest.fixture
async def spools(db_session) -> list[Spool]:
    rows = [
        Spool(material="PLA", subtype="Basic", brand="Polymaker", color_name="Ivory", rgba="F5E6D3FF"),
        Spool(material="PETG", subtype="HF", brand="Bambu", color_name="Red", rgba="FF0000FF"),
        Spool(material="PLA", subtype="Basic", brand="Polymaker", color_name="Ivory", rgba="F5E6D3FF"),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    for row in rows:
        await db_session.refresh(row)
    return rows


async def _design(db_session, *, width_mm=40.0, height_mm=30.0) -> LabelTemplate:
    row = LabelTemplate(
        name="Shelf tag",
        width_mm=width_mm,
        height_mm=height_mm,
        shape="rect",
        target="driver",
        elements=[
            {"type": "swatch", "x_mm": 2, "y_mm": 2, "w_mm": 10, "h_mm": 10, "content": "{color_hex_all}"},
            {
                "type": "text",
                "x_mm": 14,
                "y_mm": 2,
                "w_mm": 24,
                "h_mm": 6,
                "content": "{display_name}",
                "size_mm": 3,
            },
        ],
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _swatch_pixel(png: bytes, dpi: int) -> tuple[int, int, int]:
    at = round(7 * dpi / 25.4)
    return Image.open(io.BytesIO(png)).convert("RGB").getpixel((at, at))


class TestOneLabel:
    async def test_is_one_png_of_the_design_s_own_size_in_colour(self, async_client: AsyncClient, db_session, spools):
        design = await _design(db_session)

        saved = await async_client.post(
            LOCAL, json={"spools": [{"id": spools[0].id}], "template_id": design.id, "dpi": 300}
        )

        assert saved.status_code == 200, saved.text
        assert saved.headers["content-type"] == "image/png"
        assert Image.open(io.BytesIO(saved.content)).size == (472, 354)
        assert _swatch_pixel(saved.content, 300) == IVORY
        at = saved.content.index(b"pHYs")
        assert struct.unpack(">II", saved.content[at + 4 : at + 12]) == (11811, 11811)
        disposition = saved.headers["content-disposition"]
        assert disposition.startswith("attachment") and f"{spools[0].id:04d}-" in disposition and ".png" in disposition

    async def test_monochrome_drops_the_colour_block(self, async_client: AsyncClient, db_session, spools):
        design = await _design(db_session)

        saved = await async_client.post(
            LOCAL, json={"spools": [{"id": spools[0].id}], "template_id": design.id, "dpi": 203, "monochrome": True}
        )

        assert saved.status_code == 200, saved.text
        assert _swatch_pixel(saved.content, 203) == (255, 255, 255)

    async def test_one_of_the_six_names_still_answers(self, async_client: AsyncClient, spools):
        saved = await async_client.post(LOCAL, json={"spools": [{"id": spools[0].id}], "template": "box_40x30"})

        assert saved.status_code == 200, saved.text
        # 300 dpi when none is named.
        assert Image.open(io.BytesIO(saved.content)).size == (472, 354)


class TestSeveralLabels:
    async def test_are_a_zip_named_by_spool_in_the_order_asked(self, async_client: AsyncClient, db_session, spools):
        design = await _design(db_session)
        first, second, third = spools

        saved = await async_client.post(
            LOCAL,
            json={
                "spools": [
                    {"id": second.id, "display_name": "Bambu PETG Red"},
                    {"id": first.id, "display_name": "Ivory"},
                    {"id": third.id, "display_name": "Ivory"},
                ],
                "template_id": design.id,
                "dpi": 203,
            },
        )

        assert saved.status_code == 200, saved.text
        assert saved.headers["content-type"] == "application/zip"
        assert ".zip" in saved.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(saved.content)) as archive:
            assert archive.namelist() == [
                f"{second.id:04d}-bambu-petg-red.png",
                f"{first.id:04d}-ivory.png",
                f"{third.id:04d}-ivory.png",
            ]
            assert _swatch_pixel(archive.read(archive.namelist()[0]), 203) == (255, 0, 0)
            assert _swatch_pixel(archive.read(archive.namelist()[1]), 203) == IVORY

    async def test_a_design_on_a_sheet_is_saved_label_by_label_not_as_the_page(
        self, async_client: AsyncClient, db_session, spools
    ):
        design = await _design(db_session, width_mm=60.0, height_mm=25.0)  # fits a 66.7 × 25.4 mm cell
        sheet = LabelSheet(**BUILTIN_SHEETS[0])
        db_session.add(sheet)
        await db_session.commit()
        await db_session.refresh(sheet)

        saved = await async_client.post(
            LOCAL,
            json={
                "spools": [{"id": spools[0].id}, {"id": spools[1].id}],
                "template_id": design.id,
                "sheet_id": sheet.id,
                "starting_position": 4,
                "dpi": 203,
            },
        )

        assert saved.status_code == 200, saved.text
        with zipfile.ZipFile(io.BytesIO(saved.content)) as archive:
            sizes = {Image.open(io.BytesIO(archive.read(name))).size for name in archive.namelist()}
        assert len(archive.namelist()) == 2 and sizes == {(480, 200)}


class TestRefusals:
    @pytest.fixture
    def no_drawing(self, monkeypatch):
        def reached(*_args, **_kwargs):
            raise AssertionError("the renderer was reached")

        monkeypatch.setattr(label_picture, "render_pictures", reached)

    async def test_a_resolution_nobody_offers(self, async_client: AsyncClient, db_session, spools, no_drawing):
        design = await _design(db_session)
        refused = await async_client.post(
            LOCAL, json={"spools": [{"id": spools[0].id}], "template_id": design.id, "dpi": 150}
        )
        assert refused.status_code == 422

    async def test_a_design_too_large_for_the_resolution(
        self, async_client: AsyncClient, db_session, spools, no_drawing
    ):
        design = await _design(db_session, width_mm=200.0, height_mm=60.0)
        body = {"spools": [{"id": spools[0].id}], "template_id": design.id}

        refused = await async_client.post(LOCAL, json={**body, "dpi": 600})

        assert refused.status_code == 422, refused.text
        assert "600 dpi" in refused.json()["detail"]

    async def test_the_same_design_at_a_lower_resolution_is_saved(self, async_client: AsyncClient, db_session, spools):
        design = await _design(db_session, width_mm=200.0, height_mm=60.0)
        saved = await async_client.post(
            LOCAL, json={"spools": [{"id": spools[0].id}], "template_id": design.id, "dpi": 300}
        )
        assert saved.status_code == 200, saved.text

    async def test_more_labels_than_one_export_takes(self, async_client: AsyncClient, db_session, no_drawing):
        design = await _design(db_session)
        many = [{"id": number} for number in range(1, label_picture.MAX_PICTURES + 2)]

        refused = await async_client.post(LOCAL, json={"spools": many, "template_id": design.id, "dpi": 203})

        # Said before the spools are even looked up: none of these ids exists.
        assert refused.status_code == 422, refused.text
        assert str(label_picture.MAX_PICTURES) in refused.json()["detail"]

    async def test_more_bytes_than_one_export_carries(self, async_client: AsyncClient, db_session, spools, monkeypatch):
        design = await _design(db_session)
        monkeypatch.setattr(label_picture, "MAX_BUNDLE_BYTES", 100)

        refused = await async_client.post(
            LOCAL, json={"spools": [{"id": spools[0].id}, {"id": spools[1].id}], "template_id": design.id, "dpi": 203}
        )

        assert refused.status_code == 413, refused.text

    async def test_an_unknown_spool(self, async_client: AsyncClient, db_session, no_drawing):
        design = await _design(db_session)
        refused = await async_client.post(LOCAL, json={"spools": [{"id": 987654}], "template_id": design.id})
        assert refused.status_code == 404

    async def test_someone_who_may_not_print_labels(self, async_client: AsyncClient, db_session, spools, no_drawing):
        design = await _design(db_session)
        group = Group(name="Queue only", permissions=["queue:read"])
        user = User(username="queue", password_hash=get_password_hash("TestPass123!"), role="user")
        db_session.add_all([group, user])
        await db_session.commit()
        await db_session.refresh(user)
        await db_session.execute(
            text("INSERT INTO user_groups (user_id, group_id) VALUES (:uid, :gid)"), {"uid": user.id, "gid": group.id}
        )
        await db_session.commit()
        headers = {"Authorization": f"Bearer {await mint_test_access(db_session, user=user)}"}
        body = {"spools": [{"id": spools[0].id}], "template_id": design.id}

        assert (await async_client.post(LOCAL, json=body, headers=headers)).status_code == 403
        assert (await async_client.post(SPOOLMAN, json=body, headers=headers)).status_code == 403


async def test_the_pictures_are_drawn_in_a_thread(async_client: AsyncClient, db_session, spools, monkeypatch):
    design = await _design(db_session)
    real = label_picture.render_pictures
    seen: list[int] = []

    def spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(label_picture, "render_pictures", spy)

    saved = await async_client.post(LOCAL, json={"spools": [{"id": spools[0].id}], "template_id": design.id})

    assert saved.status_code == 200, saved.text
    assert seen and all(ident != threading.get_ident() for ident in seen)


class _Spoolman:
    is_connected = True

    async def get_all_spools(self, allow_archived: bool = False):
        return [
            {
                "id": number,
                "filament": {"name": name, "material": "PLA", "color_hex": colour, "vendor": {"name": "PM"}},
            }
            for number, name, colour in ((1, "PolyLite Red", "FF0000"), (2, "PolyLite Green", "00FF00"))
        ]


class TestSpoolman:
    async def test_a_spoolman_spool_is_saved_the_same_way(self, async_client: AsyncClient, db_session):
        design = await _design(db_session)
        db_session.add(Settings(key="spoolman_enabled", value="true"))
        await db_session.commit()

        with patch("backend.app.api.routes.labels.get_spoolman_client", AsyncMock(return_value=_Spoolman())):
            one = await async_client.post(SPOOLMAN, json={"spools": [{"id": 2}], "template_id": design.id, "dpi": 203})
            both = await async_client.post(
                SPOOLMAN, json={"spools": [{"id": 2}, {"id": 1}], "template_id": design.id, "dpi": 203}
            )

        assert one.status_code == 200, one.text
        assert one.headers["content-type"] == "image/png"
        assert _swatch_pixel(one.content, 203) == (0, 255, 0)
        with zipfile.ZipFile(io.BytesIO(both.content)) as archive:
            assert [name[:5] for name in archive.namelist()] == ["0002-", "0001-"]

    async def test_is_refused_while_spoolman_is_off(self, async_client: AsyncClient, db_session):
        design = await _design(db_session)
        refused = await async_client.post(SPOOLMAN, json={"spools": [{"id": 1}], "template_id": design.id})
        assert refused.status_code == 400
