"""Integration smoke for the spool labels route (B.1 — port of upstream #809).

Pinned here:
- Round-trip: ``POST /inventory/labels`` with ``{spools: [{id, display_name?}],
  template}`` returns ``application/pdf`` bytes that start with ``%PDF``.
- The per-spool ``display_name`` override is honoured — when the frontend
  forwards what ``formatSpoolDisplayName`` produced, the value reaches the
  PDF (renderer-side) instead of being rebuilt from raw fields. We verify
  the override path without compression so the bytes are greppable.
- 404 on unknown spool id; 400 on unknown template; 422 on empty list.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool


@pytest.fixture
async def spool_factory(db_session: AsyncSession):
    _counter = [0]

    async def _create(**kwargs):
        _counter[0] += 1
        defaults = {
            "material": "PLA",
            "subtype": "Basic",
            "brand": "Polymaker",
            "color_name": "Ivory",
            "rgba": "F5E6D3FF",
            "label_weight": 1000,
            "weight_used": 0,
        }
        defaults.update(kwargs)
        spool = Spool(**defaults)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        return spool

    return _create


class TestInventoryLabels:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_pdf_for_valid_template_and_ids(self, async_client: AsyncClient, spool_factory):
        a = await spool_factory()
        b = await spool_factory(color_name="Black", rgba="0E0E0EFF")
        resp = await async_client.post(
            "/api/v1/inventory/labels",
            json={
                "spools": [{"id": a.id}, {"id": b.id}],
                "template": "box_62x29",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_404_on_missing_spool_id(self, async_client: AsyncClient, spool_factory):
        a = await spool_factory()
        resp = await async_client.post(
            "/api/v1/inventory/labels",
            json={
                "spools": [{"id": a.id}, {"id": 999_999}],
                "template": "ams_holder_74x33",
            },
        )
        assert resp.status_code == 404
        assert "999999" in resp.text or "999,999" in resp.text or "[999999]" in resp.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_422_on_empty_list(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [], "template": "ams_holder_74x33"},
        )
        # Pydantic min_length=1 fails the body before route runs.
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_422_on_unknown_template(self, async_client: AsyncClient, spool_factory):
        a = await spool_factory()
        resp = await async_client.post(
            "/api/v1/inventory/labels",
            json={"spools": [{"id": a.id}], "template": "wat"},
        )
        # Literal type rejection by Pydantic before the explicit guard runs.
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_display_name_override_reaches_pdf(self, async_client: AsyncClient, spool_factory, monkeypatch):
        """The frontend composes the bold central label line via
        ``formatSpoolDisplayName(spool, settings.spool_display_template)`` so
        the printed label respects the user's naming rules. Pin that the
        forwarded ``display_name`` reaches the renderer instead of being
        rebuilt from raw fields. We force-render with pageCompression=0 so
        the bytes are greppable.
        """
        # Patch the canvas constructor used by render_labels so the test PDF
        # is uncompressed; ReportLab honours pageCompression=0 only at
        # construction time. We replace the canvas factory in the renderer
        # module so the route's call path picks up the override.
        from reportlab.pdfgen import canvas as rl_canvas

        from backend.app.services import label_renderer as lr

        original = rl_canvas.Canvas

        def _uncompressed_canvas(*args, **kwargs):
            kwargs["pageCompression"] = 0
            return original(*args, **kwargs)

        monkeypatch.setattr(lr.rl_canvas, "Canvas", _uncompressed_canvas)

        a = await spool_factory(color_name="ZNeverPicked")
        # Short override — fits the box-label text column without truncation
        # so the anchor reaches the PDF intact. The `Z`-prefix on the spool's
        # color_name guarantees the renderer can't accidentally pick it up
        # via the fallback chain (`color_name → slicer_filament_name → brand
        # material`); only the override should land on the bold central line.
        custom = "TAGX1"
        resp = await async_client.post(
            "/api/v1/inventory/labels",
            json={
                "spools": [{"id": a.id, "display_name": custom}],
                "template": "box_62x29",
            },
        )
        assert resp.status_code == 200, resp.text
        # Override wins — the user's composed name is on the label.
        assert b"TAGX1" in resp.content, "display_name override should reach the rendered PDF"
        # Raw color_name was NOT used as the bold line. (color_name is not
        # one of the renderer's separate text rows on the box template, so
        # we don't expect it anywhere in the PDF either.)
        assert b"ZNeverPicked" not in resp.content, "Backend fallback chain must NOT win when display_name is provided"
