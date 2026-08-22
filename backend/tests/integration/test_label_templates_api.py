"""The design endpoints: what they let through, and what they refuse."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from PIL import Image

from backend.app.models.label_template import LabelSheet, LabelTemplate
from backend.app.models.spool import Spool
from backend.app.services.label_seed import BUILTIN_SHEETS, BUILTIN_TEMPLATES

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _design(name: str = "Shelf tag") -> dict:
    return {
        "name": name,
        "width_mm": 40.0,
        "height_mm": 20.0,
        "shape": "rect",
        "elements": [
            {
                "type": "text",
                "x_mm": 1.5,
                "y_mm": 1.5,
                "w_mm": 25.0,
                "h_mm": 4.0,
                "content": "{brand} {material}",
                "size_mm": 4.0,
            },
            {"type": "qr", "x_mm": 28.0, "y_mm": 1.5, "w_mm": 10.0, "h_mm": 10.0, "content": "{deeplink}"},
        ],
    }


async def _seed_builtin(db_session) -> LabelTemplate:
    row = LabelTemplate(**BUILTIN_TEMPLATES[2])
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


class TestCatalog:
    async def test_a_fresh_list_is_empty_rather_than_missing(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/label-templates")
        assert response.status_code == 200
        assert response.json() == []

    async def test_a_design_round_trips(self, async_client: AsyncClient):
        created = await async_client.post("/api/v1/label-templates", json=_design())
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["is_builtin"] is False
        assert body["builtin_key"] is None
        assert len(body["elements"]) == 2

        fetched = await async_client.get(f"/api/v1/label-templates/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["elements"] == body["elements"]

    async def test_builtins_sort_ahead_of_anything_a_person_made(self, async_client: AsyncClient, db_session):
        """A fresh install prints the built-ins, so they are what a list is for."""
        await _seed_builtin(db_session)
        await async_client.post("/api/v1/label-templates", json=_design("AAA sorts first alphabetically"))

        rows = (await async_client.get("/api/v1/label-templates")).json()
        assert rows[0]["is_builtin"] is True

    async def test_an_element_type_that_does_not_exist_is_refused(self, async_client: AsyncClient):
        design = _design()
        design["elements"] = [{"type": "hologram", "x_mm": 0, "y_mm": 0, "w_mm": 5, "h_mm": 5, "content": "x"}]
        response = await async_client.post("/api/v1/label-templates", json=design)
        assert response.status_code == 422

    async def test_a_label_with_no_size_is_refused(self, async_client: AsyncClient):
        design = _design()
        design["width_mm"] = 0
        assert (await async_client.post("/api/v1/label-templates", json=design)).status_code == 422

    async def test_deleting_one_removes_it(self, async_client: AsyncClient):
        created = (await async_client.post("/api/v1/label-templates", json=_design())).json()
        assert (await async_client.delete(f"/api/v1/label-templates/{created['id']}")).status_code == 204
        assert (await async_client.get(f"/api/v1/label-templates/{created['id']}")).status_code == 404


class TestTheSeededOnesAreAStartingSet:
    """⚠️ These used to be frozen, and the reversal is deliberate.

    The reasoning for freezing them was that a key is a name the label API
    accepts, so an automation printing the same label for a year must not find
    it redrawn. That protected the wrong thing: the four seeded designs are the
    only ones most people ever see, and they were the only ones nobody could
    adjust — so every small change meant "duplicate, edit the copy", leaving two
    rows that look almost the same and one of them still wrong.

    An edit now reaches the automations too, which is the point of it. The key
    keeps its two other jobs: resolving those names, and saying where a row came
    from.
    """

    async def test_a_seeded_design_can_be_edited(self, async_client: AsyncClient, db_session):
        row = await _seed_builtin(db_session)

        response = await async_client.put(f"/api/v1/label-templates/{row.id}", json=_design("Redrawn"))

        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Redrawn"

    async def test_editing_one_keeps_its_key(self, async_client: AsyncClient, db_session):
        """⚠️ Losing the key would take the name out of the label API, which is
        the opposite of what editing is for: the automation should print the
        redrawn label, not stop finding one."""
        row = await _seed_builtin(db_session)

        edited = await async_client.put(f"/api/v1/label-templates/{row.id}", json=_design("Redrawn"))

        assert edited.json()["builtin_key"] == row.builtin_key
        assert edited.json()["is_builtin"] is True

    async def test_a_seeded_design_can_be_deleted(self, async_client: AsyncClient, db_session):
        row = await _seed_builtin(db_session)

        assert (await async_client.delete(f"/api/v1/label-templates/{row.id}")).status_code == 204
        assert (await async_client.get(f"/api/v1/label-templates/{row.id}")).status_code == 404

    async def test_duplicating_a_builtin_drops_its_key(self, async_client: AsyncClient, db_session):
        """Two rows answering to ``box_40x30`` would make which label prints a
        coin toss — and the column is UNIQUE, so the copy would fail to save.
        """
        row = await _seed_builtin(db_session)
        copy = await async_client.post(f"/api/v1/label-templates/{row.id}/duplicate")
        assert copy.status_code == 201, copy.text
        body = copy.json()
        assert body["builtin_key"] is None
        assert body["is_builtin"] is False
        assert body["id"] != row.id

        # Compared through the API on both sides. The seed stores partial dicts
        # and a response fills in every default, so reading one raw and one
        # served would report a difference that is not there.
        source = (await async_client.get(f"/api/v1/label-templates/{row.id}")).json()
        assert body["elements"] == source["elements"]

    async def test_a_copy_is_editable(self, async_client: AsyncClient, db_session):
        """Duplicating is no longer the only way to change one, but it is still
        how you keep the original beside a variant."""
        row = await _seed_builtin(db_session)
        copy = (await async_client.post(f"/api/v1/label-templates/{row.id}/duplicate")).json()
        edited = await async_client.put(f"/api/v1/label-templates/{copy['id']}", json=_design("Mine"))
        assert edited.status_code == 200
        assert edited.json()["name"] == "Mine"


class TestVocabulary:
    async def test_the_placeholder_list_is_served_rather_than_duplicated(self, async_client: AsyncClient):
        """The editor's picker and the renderer have to agree about what
        ``{remaining_g}`` means; a second hand-maintained list is how they stop.
        """
        rows = (await async_client.get("/api/v1/label-templates/placeholders")).json()
        keys = {row["key"] for row in rows}
        assert {"brand", "material", "remaining_g", "deeplink", "ean"} <= keys
        assert all(row["example"] for row in rows), "a picker with no example teaches nothing"

    async def test_the_swatch_default_is_a_known_placeholder(self, async_client: AsyncClient):
        """⚠️ An unknown key survives resolution verbatim, so a swatch defaulting
        to an unknown one would try to draw a block the colour of the literal
        text "{color_hex_all}".
        """
        keys = {row["key"] for row in (await async_client.get("/api/v1/label-templates/placeholders")).json()}
        assert "color_hex_all" in keys

    async def test_sheets_are_listed(self, async_client: AsyncClient, db_session):
        db_session.add(LabelSheet(**BUILTIN_SHEETS[0]))
        await db_session.commit()
        rows = (await async_client.get("/api/v1/label-templates/sheets")).json()
        assert [row["builtin_key"] for row in rows] == ["avery_5160"]
        assert rows[0]["cols"] * rows[0]["rows"] == 30

    async def test_a_sheet_never_names_a_template(self, async_client: AsyncClient, db_session):
        """A sheet describes paper. Pointing at a design would make that design
        undeletable while a sheet looks at it.
        """
        db_session.add(LabelSheet(**BUILTIN_SHEETS[0]))
        await db_session.commit()
        rows = (await async_client.get("/api/v1/label-templates/sheets")).json()
        assert not [key for key in rows[0] if "template" in key]


class TestPreview:
    async def test_an_unsaved_design_renders(self, async_client: AsyncClient):
        """⚠️ The whole point: dragging a box must not have to save anything."""
        response = await async_client.post(
            "/api/v1/label-templates/preview",
            json={"template": _design(), "dots_per_mm": 8.0},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert (await async_client.get("/api/v1/label-templates")).json() == []

    async def test_the_picture_is_the_size_the_printer_would_print(self, async_client: AsyncClient):
        """8 dots per millimetre is 203 dpi — 40 mm is 320 dots."""
        response = await async_client.post(
            "/api/v1/label-templates/preview",
            json={"template": _design(), "dots_per_mm": 8.0},
        )
        img = Image.open(io.BytesIO(response.content))
        assert img.width == 320
        assert img.height == 160
        assert img.mode == "1", "a preview that is not 1-bit hides what only goes wrong at 1 bit"

    async def test_a_real_spool_fills_the_placeholders(self, async_client: AsyncClient, db_session):
        spool = Spool(material="PETG", brand="Polymaker", rgba="FF3300FF", label_weight=1000, weight_used=250)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)

        response = await async_client.post(
            "/api/v1/label-templates/preview",
            json={"template": _design(), "spool_id": spool.id, "dots_per_mm": 8.0},
        )
        assert response.status_code == 200
        # Ink, rather than a blank sheet: the text resolved to something.
        img = Image.open(io.BytesIO(response.content)).convert("L")
        assert min(img.getdata()) == 0

    async def test_an_unknown_spool_is_a_404_not_a_blank_label(self, async_client: AsyncClient):
        response = await async_client.post(
            "/api/v1/label-templates/preview",
            json={"template": _design(), "spool_id": 999999},
        )
        assert response.status_code == 404

    async def test_trouble_comes_back_in_a_header_and_the_picture_still_arrives(self, async_client: AsyncClient):
        """⚠️ A template with one bad element still has a picture worth looking
        at. Refusing the whole preview would leave the editor blank exactly when
        somebody needs to see what they just broke.
        """
        design = _design()
        design["elements"].append(
            {
                "type": "barcode",
                "x_mm": 1.0,
                "y_mm": 12.0,
                "w_mm": 30.0,
                "h_mm": 6.0,
                "content": "not-a-number",
                "symbology": "ean13",
            }
        )
        response = await async_client.post("/api/v1/label-templates/preview", json={"template": design})
        assert response.status_code == 200
        assert "ean13" in response.headers["x-label-warnings"]
        assert Image.open(io.BytesIO(response.content)).width > 0

    async def test_a_clean_design_carries_no_warning_header(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/label-templates/preview", json={"template": _design()})
        assert "x-label-warnings" not in response.headers
