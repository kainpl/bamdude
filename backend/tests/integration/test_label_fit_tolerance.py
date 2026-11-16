"""One answer to "does this design fit that cell" — the dialog's, the preview's and the print's.

There were three: the print dialog allowed half a millimetre over, printing
allowed a millionth, and the page preview none. A design 63.8 mm wide could be
chosen for a 63.5 mm cell, and the print it offered answered 400. The slack is
one constant now, asked through one function, and the dialog is TOLD which
designs fit rather than working it out again.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.models.label_device import LabelDevice
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


def _design(width: float, height: float = 38.0, target: str = "driver") -> dict:
    return {
        "name": f"{width:g} wide",
        "width_mm": width,
        "height_mm": height,
        "shape": "rect",
        "target": target,
        "elements": [
            {"type": "text", "x_mm": 1, "y_mm": 1, "w_mm": 20, "h_mm": 4, "content": "{brand}", "size_mm": 3},
        ],
    }


@pytest.fixture
async def a_spool(db_session) -> Spool:
    spool = Spool(material="PLA", subtype="Basic", brand="Polymaker", color_name="Ivory", rgba="F5E6D3FF")
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


@pytest.fixture
async def catalogue(async_client: AsyncClient) -> dict:
    """A sheet, a design a hair wider than its cell, and one clearly too wide."""
    sheet = (await async_client.post(SHEETS, json=A_SHEET)).json()
    snug = (await async_client.post(TEMPLATES, json=_design(63.8))).json()
    wide = (await async_client.post(TEMPLATES, json=_design(64.1))).json()
    return {"sheet": sheet, "snug": snug, "wide": wide}


def test_the_rule_is_one_function_with_one_slack():
    assert rules.FIT_TOLERANCE_MM == 0.5
    assert rules.fits_within(63.8, 38.0, 63.5, 38.1)
    assert rules.fits_within(64.0, 38.6, 63.5, 38.1)
    assert not rules.fits_within(64.1, 38.0, 63.5, 38.1)
    assert not rules.fits_within(63.5, 38.7, 63.5, 38.1)


async def test_the_sheet_list_says_which_designs_fit_its_cell(async_client: AsyncClient, catalogue):
    listed = {row["id"]: row for row in (await async_client.get(SHEETS)).json()}

    fitting = listed[catalogue["sheet"]["id"]]["fitting_design_ids"]
    assert catalogue["snug"]["id"] in fitting
    assert catalogue["wide"]["id"] not in fitting


async def test_a_design_the_list_offers_is_printed(async_client: AsyncClient, catalogue, a_spool):
    printed = await async_client.post(
        "/api/v1/inventory/labels",
        json={
            "spools": [{"id": a_spool.id}],
            "template_id": catalogue["snug"]["id"],
            "sheet_id": catalogue["sheet"]["id"],
        },
    )

    assert printed.status_code == 200, printed.text


async def test_one_it_does_not_offer_is_refused(async_client: AsyncClient, catalogue, a_spool):
    refused = await async_client.post(
        "/api/v1/inventory/labels",
        json={
            "spools": [{"id": a_spool.id}],
            "template_id": catalogue["wide"]["id"],
            "sheet_id": catalogue["sheet"]["id"],
        },
    )

    assert refused.status_code == 400, refused.text


async def test_the_page_preview_warns_about_exactly_the_same_designs(async_client: AsyncClient, catalogue):
    snug = await async_client.post(f"{SHEETS}/preview", json={"sheet": A_SHEET, "template_id": catalogue["snug"]["id"]})
    wide = await async_client.post(f"{SHEETS}/preview", json={"sheet": A_SHEET, "template_id": catalogue["wide"]["id"]})

    assert "does not fit" not in snug.headers.get("X-Label-Warnings", "")
    assert "does not fit" in wide.headers.get("X-Label-Warnings", "")


async def test_a_desk_printer_says_which_designs_fit_its_stock(async_client: AsyncClient, db_session):
    db_session.add(Settings(key="device_labels_enabled", value="true"))
    loaded = LabelDevice(
        installation_id="loaded", driver="niimbot", enabled=True, cassette_width_mm=40.0, cassette_height_mm=20.0
    )
    silent = LabelDevice(installation_id="silent", driver="niimbot", enabled=True)
    db_session.add_all([loaded, silent])
    await db_session.commit()
    snug = (await async_client.post(TEMPLATES, json=_design(40.4, 20.0, target="thermal"))).json()
    wide = (await async_client.post(TEMPLATES, json=_design(40.6, 20.0, target="thermal"))).json()

    devices = {row["installation_id"]: row for row in (await async_client.get("/api/v1/label-devices")).json()}

    assert snug["id"] in devices["loaded"]["fitting_design_ids"]
    assert wide["id"] not in devices["loaded"]["fitting_design_ids"]
    # ⚠️ Not judged, which is not the same as "nothing fits": a printer that has
    # not said what is loaded cannot rule on a design.
    assert devices["silent"]["fitting_design_ids"] is None
