"""What a label door refuses before it draws, and where the drawing runs.

A design and a sheet are data a caller sends, and until these bounds existed the
only ceilings were the label's own 500 mm and the preview's dots per millimetre:
a grid of a hundred million micro-cells, a font a hundred metres tall and a
``NaN`` coordinate all reached the renderer — on the event loop, where one such
request stops the whole server. Every door that takes geometry (save, preview,
print) asks the same two functions, and the drawing itself runs in a thread.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time

import pytest
from httpx import AsyncClient
from PIL import Image

from backend.app.models.label_device import LabelDevice
from backend.app.models.label_template import LabelSheet, LabelTemplate
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.services import label_template as rules

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

TEMPLATES = "/api/v1/label-templates"
SHEETS = "/api/v1/label-templates/sheets"

A_SHEET = {
    "name": "My stock",
    "page_size": "A4",
    "cell_width_mm": 63.5,
    "cell_height_mm": 38.1,
    "cols": 3,
    "rows": 7,
    "margin_top_mm": 15.0,
    "margin_left_mm": 7.0,
    "gap_x_mm": 2.5,
    "gap_y_mm": 0.0,
}

#: The audit's Д1: 10⁸ cells of 0.02 mm, which fit an A4 page on both axes.
MICRO_GRID = {
    **A_SHEET,
    "cell_width_mm": 0.02,
    "cell_height_mm": 0.02,
    "cols": 10_000,
    "rows": 10_000,
    "margin_top_mm": 0.0,
    "margin_left_mm": 0.0,
    "gap_x_mm": 0.0,
}


def _text(**overrides) -> dict:
    return {
        "type": "text",
        "x_mm": 1.5,
        "y_mm": 1.5,
        "w_mm": 25.0,
        "h_mm": 4.0,
        "content": "{brand} {material}",
        "size_mm": 4.0,
        **overrides,
    }


def _design(*elements: dict, **overrides) -> dict:
    return {
        "name": "Shelf tag",
        "width_mm": 40.0,
        "height_mm": 20.0,
        "shape": "rect",
        "elements": list(elements) or [_text()],
        **overrides,
    }


def _json_with(literal: str, design: dict, field: str) -> str:
    """The design as JSON text with one number replaced by a bare literal.

    ``NaN`` and ``Infinity`` are not JSON, but Python's parser reads them and so
    does the server's — which is how they arrive.
    """
    import json

    marker = 987654.25
    patched = {**design, "elements": [{**design["elements"][0], field: marker}, *design["elements"][1:]]}
    return json.dumps({"template": patched}).replace(str(marker), literal)


@pytest.fixture
async def a_spool(db_session) -> Spool:
    spool = Spool(material="PLA", subtype="Basic", brand="Polymaker", color_name="Ivory", rgba="F5E6D3FF")
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


@pytest.fixture
async def a_template(async_client: AsyncClient) -> dict:
    created = await async_client.post(TEMPLATES, json=_design())
    assert created.status_code == 201, created.text
    return created.json()


@pytest.fixture
def no_drawing(monkeypatch):
    """Fail the test if any renderer is reached: a refusal must come first."""
    from backend.app.api.routes import label_templates as templates_route, labels as labels_route
    from backend.app.services import label_renderer

    def reached(*_args, **_kwargs):
        raise AssertionError("the renderer was reached")

    monkeypatch.setattr(label_renderer, "render_template_sheet_pdf", reached)
    monkeypatch.setattr(labels_route, "render_template_sheet_pdf", reached)
    monkeypatch.setattr(labels_route, "render_template_pdf", reached)
    monkeypatch.setattr(templates_route, "render_template_png", reached)


class TestAnAbsurdGridIsRefusedBeforeAnythingIsDrawn:
    async def test_a_hundred_million_micro_cells_in_the_page_preview(
        self, async_client: AsyncClient, a_template, no_drawing
    ):
        refused = await async_client.post(
            f"{SHEETS}/preview", json={"sheet": MICRO_GRID, "template_id": a_template["id"]}
        )

        assert refused.status_code == 422, refused.text

    async def test_the_same_grid_is_not_saved(self, async_client: AsyncClient):
        assert (await async_client.post(SHEETS, json=MICRO_GRID)).status_code == 422

    async def test_a_stored_one_is_not_printed(
        self, async_client: AsyncClient, db_session, a_template, a_spool, no_drawing
    ):
        """A row from before the bounds, or one written past the API."""
        row = LabelSheet(builtin_key=None, **MICRO_GRID)
        db_session.add(row)
        await db_session.commit()

        refused = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template_id": a_template["id"], "sheet_id": row.id},
        )

        assert refused.status_code == 422, refused.text

    @pytest.mark.parametrize(
        "change",
        [
            {"cell_width_mm": 4.9},
            {"cell_height_mm": 4.9},
            {"margin_left_mm": 2000.5, "cols": 1},
        ],
    )
    async def test_a_cell_or_a_length_outside_the_bounds_is_refused(self, async_client: AsyncClient, change):
        assert (await async_client.post(SHEETS, json={**A_SHEET, **change})).status_code == 422

    async def test_the_bounds_are_the_agreed_ones(self):
        assert (rules.MAX_SHEET_COLS, rules.MAX_SHEET_ROWS) == (50, 100)
        assert (rules.MIN_CELL_SIDE_MM, rules.MAX_SHEET_LENGTH_MM) == (5.0, 2000.0)

    @pytest.mark.parametrize("change", [{"cols": 51}, {"rows": 101}])
    async def test_too_many_columns_or_rows_is_said_whatever_the_paper(self, change):
        """On today's three papers a 5 mm cell already caps the grid below these;
        the rule is stated on its own so a wider paper cannot lift it."""
        sheet = rules.LabelSheetSpec(**{**A_SHEET, "cell_width_mm": 5.0, "cell_height_mm": 5.0, **change})

        assert rules.sheet_problems(sheet)


class TestAnAbsurdDesignIsRefused:
    @pytest.mark.parametrize(
        "element",
        [
            _text(w_mm=500.5),
            _text(h_mm=500.5),
            _text(size_mm=50.5),
            _text(size_mm=100_000.0),
            _text(x_mm=1e9),
            _text(y_mm=-1e9),
        ],
    )
    async def test_on_save_and_in_the_preview(self, async_client: AsyncClient, element, no_drawing):
        design = _design(element)

        assert (await async_client.post(TEMPLATES, json=design)).status_code == 422
        assert (await async_client.post(f"{TEMPLATES}/preview", json={"template": design})).status_code == 422

    async def test_more_type_than_a_label_holds(self, async_client: AsyncClient, no_drawing):
        """Each box is within its own ceiling; together they are a minute of shrink-to-fit."""
        design = _design(*[_text(size_mm=rules.MAX_FONT_SIZE_MM) for _ in range(11)])

        assert (await async_client.post(f"{TEMPLATES}/preview", json={"template": design})).status_code == 422

    async def test_more_elements_than_a_label_holds(self, async_client: AsyncClient, no_drawing):
        design = _design(*[_text() for _ in range(rules.MAX_ELEMENTS + 1)])

        assert (await async_client.post(f"{TEMPLATES}/preview", json={"template": design})).status_code == 422

    async def test_a_picture_larger_than_any_printhead_draws(self, async_client: AsyncClient, no_drawing):
        """500 mm at 40 dots per millimetre is a 20 000-dot side."""
        design = _design(width_mm=500.0, height_mm=500.0)

        refused = await async_client.post(f"{TEMPLATES}/preview", json={"template": design, "dots_per_mm": 40})

        assert refused.status_code == 422, refused.text

    async def test_a_sane_design_still_previews(self, async_client: AsyncClient):
        shown = await async_client.post(f"{TEMPLATES}/preview", json={"template": _design()})

        assert shown.status_code == 200, shown.text
        assert shown.headers["content-type"] == "image/png"

    async def test_a_stored_one_still_lists_and_is_not_printed(
        self, async_client: AsyncClient, db_session, a_spool, no_drawing
    ):
        row = LabelTemplate(
            name="Old",
            width_mm=40.0,
            height_mm=20.0,
            shape="rect",
            target="driver",
            elements=[_text(size_mm=100_000.0)],
            builtin_key=None,
        )
        db_session.add(row)
        await db_session.commit()

        listed = await async_client.get(TEMPLATES)
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == row.id for item in listed.json())

        refused = await async_client.post(
            "/api/v1/inventory/labels", json={"spools": [{"id": a_spool.id}], "template_id": row.id}
        )
        assert refused.status_code == 422, refused.text


class TestANumberThatIsNotOne:
    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    @pytest.mark.parametrize("field", ["x_mm", "y_mm", "w_mm", "size_mm"])
    async def test_is_a_422_in_the_preview(self, async_client: AsyncClient, literal, field):
        refused = await async_client.post(
            f"{TEMPLATES}/preview",
            content=_json_with(literal, _design(), field),
            headers={"Content-Type": "application/json"},
        )

        assert refused.status_code == 422, refused.text

    @pytest.mark.parametrize("literal", ["NaN", "Infinity"])
    async def test_is_never_saved(self, async_client: AsyncClient, literal):
        body = _json_with(literal, _design(), "x_mm")
        refused = await async_client.post(
            TEMPLATES,
            content=body[len('{"template": ') : -1],
            headers={"Content-Type": "application/json"},
        )

        assert refused.status_code == 422, refused.text
        assert (await async_client.get(TEMPLATES)).json() == []

    @pytest.mark.parametrize("literal", ["NaN", "Infinity"])
    @pytest.mark.parametrize("field", ["cell_width_mm", "margin_left_mm", "gap_x_mm"])
    async def test_is_a_422_for_a_sheet(self, async_client: AsyncClient, literal, field):
        import json

        body = json.dumps({**A_SHEET, field: 987654.25}).replace("987654.25", literal)
        refused = await async_client.post(SHEETS, content=body, headers={"Content-Type": "application/json"})

        assert refused.status_code == 422, refused.text


class TestASheetOnItsOwnWithACellTooSmallForTheBuiltInLayout:
    async def test_is_refused_in_words(self, async_client: AsyncClient, db_session, a_spool):
        """The audit's Д6: the layout BamDude builds to the cell has no room in
        a 10 mm one, and that used to surface as a 500."""
        row = LabelSheet(
            builtin_key=None,
            **{**A_SHEET, "cell_width_mm": 10.0, "cell_height_mm": 10.0, "cols": 10, "rows": 20, "gap_x_mm": 0.0},
        )
        db_session.add(row)
        await db_session.commit()

        refused = await async_client.post(
            "/api/v1/inventory/labels", json={"spools": [{"id": a_spool.id}], "sheet_id": row.id}
        )

        assert refused.status_code == 422, refused.text
        assert "design" in refused.json()["detail"]


class TestDrawingLeavesTheEventLoop:
    async def test_the_loop_keeps_serving_while_a_pdf_is_drawn(self, async_client: AsyncClient, a_spool, monkeypatch):
        from backend.app.api.routes import labels as labels_route

        slow = 0.6
        real = labels_route.render_template_pdf
        drawn = []

        def slow_render(*args, **kwargs):
            time.sleep(slow)  # five hundred labels with a QR each
            drawn.append(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(labels_route, "render_template_pdf", slow_render)
        ticks = 0
        done = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not done.is_set():
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            started = time.monotonic()
            printed = await async_client.post(
                "/api/v1/inventory/labels", json={"spools": [{"id": a_spool.id}], "template": "box_40x30"}
            )
            elapsed = time.monotonic() - started
        finally:
            done.set()
            await beat

        assert printed.status_code == 200, printed.text
        assert drawn and drawn[0] != threading.get_ident()
        # On the loop the ticker cannot run during the render; off it, it keeps its ~50 beats a second.
        assert elapsed >= slow and ticks >= int(slow / 0.02 / 2), (ticks, elapsed)

    async def test_a_sheet_pdf_is_drawn_in_a_thread(self, async_client: AsyncClient, a_spool, monkeypatch):
        from backend.app.api.routes import labels as labels_route

        seen = _spy(monkeypatch, labels_route, "render_template_sheet_pdf")

        printed = await async_client.post(
            "/api/v1/inventory/labels", json={"spools": [{"id": a_spool.id}], "template": "avery_l7160"}
        )

        assert printed.status_code == 200, printed.text
        assert seen and all(ident != threading.get_ident() for ident in seen)

    async def test_the_editor_s_picture_is_drawn_in_a_thread(self, async_client: AsyncClient, monkeypatch):
        from backend.app.api.routes import label_templates as templates_route

        seen = _spy(monkeypatch, templates_route, "render_template_png")

        shown = await async_client.post(f"{TEMPLATES}/preview", json={"template": _design()})

        assert shown.status_code == 200, shown.text
        assert seen and all(ident != threading.get_ident() for ident in seen)

    async def test_the_page_preview_is_drawn_in_a_thread(self, async_client: AsyncClient, a_template, monkeypatch):
        from backend.app.services import label_renderer

        seen = _spy(monkeypatch, label_renderer, "render_template_sheet_pdf")

        shown = await async_client.post(f"{SHEETS}/preview", json={"sheet": A_SHEET, "template_id": a_template["id"]})

        assert shown.status_code == 200, shown.text
        assert seen and all(ident != threading.get_ident() for ident in seen)

    async def test_a_device_s_pictures_are_drawn_in_a_thread(
        self, async_client: AsyncClient, db_session, a_template, a_spool, monkeypatch
    ):
        from backend.app.services import label_dispatch

        db_session.add(Settings(key="device_labels_enabled", value="true"))
        device = LabelDevice(installation_id="bench", name="Bench", driver="niimbot", enabled=True)
        db_session.add(device)
        await db_session.commit()
        await db_session.refresh(device)

        seen = _spy(monkeypatch, label_dispatch, "render_template_png")
        body = {"device_id": device.id, "template_id": a_template["id"]}

        queued = await async_client.post("/api/v1/label-jobs", json={**body, "spools": [{"id": a_spool.id}]})
        assert queued.status_code == 201, queued.text
        shown = await async_client.post("/api/v1/label-jobs/preview", json={**body, "spool": {"id": a_spool.id}})
        assert shown.status_code == 200, shown.text

        assert len(seen) == 2 and all(ident != threading.get_ident() for ident in seen)


class TestTheEditorsPictureOfADriverDesign:
    """The audit's Д5: a design printed through a driver is a PDF in colour, and
    the one-bit picture showed it with a hole where its colour block is. Asked
    for in colour, the editor's picture is that PDF page rasterised."""

    SWATCH = {"type": "swatch", "x_mm": 2.0, "y_mm": 2.0, "w_mm": 10.0, "h_mm": 10.0, "content": "FF0000"}

    async def test_the_colour_block_is_in_the_picture(self, async_client: AsyncClient):
        body = {"template": _design(self.SWATCH), "dots_per_mm": 8.0, "colour": True}

        shown = await async_client.post(f"{TEMPLATES}/preview", json=body)

        assert shown.status_code == 200, shown.text
        picture = Image.open(io.BytesIO(shown.content)).convert("RGB")
        assert picture.size == (320, 160)
        assert picture.getpixel((56, 56)) == (255, 0, 0)

    async def test_unasked_it_stays_the_one_bit_picture_a_thermal_head_prints(self, async_client: AsyncClient):
        shown = await async_client.post(f"{TEMPLATES}/preview", json={"template": _design(), "dots_per_mm": 8.0})

        assert Image.open(io.BytesIO(shown.content)).mode == "1"

    async def test_it_is_drawn_in_a_thread_and_never_by_the_one_bit_renderer(
        self, async_client: AsyncClient, monkeypatch, no_drawing
    ):
        from backend.app.services import label_picture

        seen = _spy(monkeypatch, label_picture, "render_pictures")

        shown = await async_client.post(f"{TEMPLATES}/preview", json={"template": _design(), "colour": True})

        assert shown.status_code == 200, shown.text
        assert seen and all(ident != threading.get_ident() for ident in seen)

    async def test_one_too_large_for_the_resolution_is_refused_before_it_is_drawn(
        self, async_client: AsyncClient, monkeypatch
    ):
        from backend.app.services import label_picture

        def reached(*_args, **_kwargs):
            raise AssertionError("the renderer was reached")

        monkeypatch.setattr(label_picture, "render_pictures", reached)
        body = {"template": _design(width_mm=500.0, height_mm=500.0), "dots_per_mm": 40, "colour": True}

        refused = await async_client.post(f"{TEMPLATES}/preview", json=body)

        assert refused.status_code == 422, refused.text


def _spy(monkeypatch, module, name: str) -> list[int]:
    """Record which thread each call of ``module.name`` ran on."""
    real = getattr(module, name)
    seen: list[int] = []

    def wrapped(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapped)
    return seen
