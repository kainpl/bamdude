"""The six old names, after the layouts became templates.

⚠️ This is a public contract reachable by API key. A caller that knows nothing
about templates must not notice this change at all.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.label_template import LabelSheet, LabelTemplate
from backend.app.models.spool import Spool
from backend.app.services.label_seed import BUILTIN_SHEETS, BUILTIN_TEMPLATES

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

OLD_NAMES = ("ams_holder_74x33", "ams_holder_75x55", "box_40x30", "box_62x29", "avery_5160", "avery_l7160")


@pytest.fixture
async def a_spool(db_session: AsyncSession) -> Spool:
    spool = Spool(
        material="PLA",
        subtype="Matte",
        brand="Polymaker",
        color_name="Ivory",
        rgba="F5E6D3FF",
        label_weight=1000,
        weight_used=250,
        note="Kitchen shelf",
    )
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


@pytest.fixture
async def seeded(db_session: AsyncSession) -> dict[str, int]:
    """What m146 puts in the database, without running migrations."""
    ids: dict[str, int] = {}
    for row in BUILTIN_TEMPLATES:
        template = LabelTemplate(**row)
        db_session.add(template)
    for row in BUILTIN_SHEETS:
        db_session.add(LabelSheet(**row))
    await db_session.commit()

    from sqlalchemy import select

    for template in (await db_session.execute(select(LabelTemplate))).scalars():
        ids[template.builtin_key] = template.id
    for sheet in (await db_session.execute(select(LabelSheet))).scalars():
        ids[sheet.builtin_key] = sheet.id
    return ids


class TestTheOldNamesStillWork:
    @pytest.mark.parametrize("name", OLD_NAMES)
    async def test_every_one_still_returns_a_pdf(self, async_client: AsyncClient, a_spool, seeded, name):
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template": name},
        )
        assert response.status_code == 200, response.text
        assert response.content.startswith(b"%PDF-")

    @pytest.mark.parametrize("name", OLD_NAMES)
    async def test_every_one_works_before_the_seed_has_run(self, async_client: AsyncClient, a_spool, name):
        """⚠️ Deliberately without the ``seeded`` fixture. The route falls back to
        the seed constants when a row is missing, so a database mid-upgrade
        cannot turn a working print button into a 500.
        """
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template": name},
        )
        assert response.status_code == 200, response.text

    async def test_an_unknown_name_is_still_refused_before_anything_is_loaded(self, async_client: AsyncClient, a_spool):
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template": "wat"},
        )
        assert response.status_code == 422

    async def test_monochrome_still_drops_the_colour_block(self, async_client: AsyncClient, a_spool, seeded):
        """On a black-and-white thermal head a colour block prints as a muddy
        grey rectangle; the hex line carries the colour instead (#1870).
        """
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template": "box_40x30", "monochrome": True},
        )
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")

    async def test_a_sheet_name_lays_out_a_page(self, async_client: AsyncClient, db_session, seeded):
        """Thirty spools on an Avery 5160 is one page, not thirty."""
        spools = []
        for index in range(30):
            spool = Spool(material="PLA", brand="B", color_name=f"C{index}", label_weight=1000, weight_used=0)
            db_session.add(spool)
            spools.append(spool)
        await db_session.commit()
        for spool in spools:
            await db_session.refresh(spool)

        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": s.id} for s in spools], "template": "avery_5160"},
        )
        assert response.status_code == 200
        assert response.content.count(b"/Type /Page\n") <= 1 or b"/Count 1" in response.content


class TestNamingADesignDirectly:
    async def test_a_template_id_can_be_used_instead_of_a_name(self, async_client: AsyncClient, a_spool, seeded):
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template_id": seeded["box_40x30"]},
        )
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")

    async def test_naming_both_is_refused(self, async_client: AsyncClient, a_spool, seeded):
        """Two answers to one question; guessing which the caller meant would
        print a batch of the wrong label.
        """
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={
                "spools": [{"id": a_spool.id}],
                "template": "box_40x30",
                "template_id": seeded["box_40x30"],
            },
        )
        assert response.status_code == 422

    async def test_naming_neither_is_refused(self, async_client: AsyncClient, a_spool):
        response = await async_client.post("/api/v1/inventory/labels", json={"spools": [{"id": a_spool.id}]})
        assert response.status_code == 422

    async def test_an_unknown_template_id_is_a_404(self, async_client: AsyncClient, a_spool):
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template_id": 987654},
        )
        assert response.status_code == 404

    async def test_any_design_can_go_on_any_paper(self, async_client: AsyncClient, a_spool, db_session, seeded):
        """The pairing the six fixed names could never express."""
        small = LabelTemplate(name="Tiny", width_mm=40.0, height_mm=20.0, shape="rect", elements=[])
        db_session.add(small)
        await db_session.commit()
        await db_session.refresh(small)

        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={
                "spools": [{"id": a_spool.id}],
                "template_id": small.id,
                "sheet_id": seeded["avery_l7160"],
            },
        )
        assert response.status_code == 200

    async def test_a_design_too_big_for_the_cell_is_refused_rather_than_clipped(
        self, async_client: AsyncClient, a_spool, seeded
    ):
        """⚠️ Silently overlapping the neighbouring cell wastes a whole sheet of
        stock, and the damage is only visible on paper.
        """
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={
                "spools": [{"id": a_spool.id}],
                "template_id": seeded["ams_holder_75x55"],
                "sheet_id": seeded["avery_l7160"],
            },
        )
        assert response.status_code == 400
        assert "does not fit" in response.json()["detail"]

    async def test_one_of_the_six_names_beside_a_sheet_is_refused(self, async_client: AsyncClient, a_spool, seeded):
        """⚠️ Two of those names ARE sheets, so this answers "which paper"
        twice. Ignoring the loser is the quiet failure — a batch on the wrong
        stock — so it is refused instead."""
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template": "box_40x30", "sheet_id": seeded["avery_5160"]},
        )
        assert response.status_code == 422

    async def test_a_sheet_on_its_own_prints_a_page_of_them(self, async_client: AsyncClient, a_spool, seeded):
        """⚠️ It used to be refused, and it should never have been: the legacy
        name ``avery_5160`` does exactly this — builds the design to the cell —
        so refusing the same request by id made the newer path the weaker one,
        and forced the print dialog to demand a design it does not need."""
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "sheet_id": seeded["avery_5160"]},
        )

        assert response.status_code == 200, response.text
        assert response.content.startswith(b"%PDF")

    async def test_naming_nothing_at_all_is_still_refused(self, async_client: AsyncClient, a_spool):
        response = await async_client.post("/api/v1/inventory/labels", json={"spools": [{"id": a_spool.id}]})

        assert response.status_code == 422


class TestTheNameOnTheLabel:
    """⚠️ Interpolation moved server-side, and this is why: a caller with no
    browser must get the label the page would have printed, not a different one.
    """

    @staticmethod
    def _uncompressed(monkeypatch) -> None:
        from reportlab.pdfgen import canvas as rl_canvas

        from backend.app.services import label_renderer as lr

        original = rl_canvas.Canvas

        def _factory(*args, **kwargs):
            kwargs["pageCompression"] = 0
            return original(*args, **kwargs)

        monkeypatch.setattr(lr.rl_canvas, "Canvas", _factory)

    async def test_the_naming_setting_is_applied_when_no_name_is_sent(
        self, async_client: AsyncClient, a_spool, seeded, db_session, monkeypatch
    ):
        self._uncompressed(monkeypatch)
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="spool_display_template", value="{brand} {subtype}"))
        await db_session.commit()

        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template": "box_62x29"},
        )
        assert response.status_code == 200
        assert b"Polymaker Matte" in response.content

    async def test_a_name_from_the_page_still_wins(
        self, async_client: AsyncClient, a_spool, seeded, db_session, monkeypatch
    ):
        self._uncompressed(monkeypatch)
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="spool_display_template", value="{brand} {subtype}"))
        await db_session.commit()

        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id, "display_name": "TAGX1"}], "template": "box_62x29"},
        )
        assert response.status_code == 200
        assert b"TAGX1" in response.content

    async def test_a_label_is_never_blank(self, async_client: AsyncClient, a_spool, seeded):
        """A caller sending no name at all still gets a filled-in label."""
        response = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a_spool.id}], "template": "box_40x30"},
        )
        assert response.status_code == 200
        assert len(response.content) > 1000
