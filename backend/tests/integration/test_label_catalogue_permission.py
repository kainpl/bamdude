"""Whoever may print labels may see what there is to print them with.

Printing asks ``inventory:read``; the catalogue the print dialog lists — the
designs and the sheets — asked ``label_templates:read``. In the three default
groups the two travel together, so nothing showed. A group built by hand with
the first and not the second got a dialog that said "no designs yet" over a
catalogue full of them, while the print API answered the same person by name.

Reading the two lists now takes either right. Everything that DRAWS or WRITES a
design keeps its own: the editor's preview, saving, deleting.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from backend.app.core.auth import get_password_hash
from backend.app.models.group import Group
from backend.app.models.label_template import LabelSheet, LabelTemplate
from backend.app.models.spool import Spool
from backend.app.models.user import User
from backend.app.services.label_seed import BUILTIN_SHEETS, BUILTIN_TEMPLATES
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

TEMPLATES = "/api/v1/label-templates"
SHEETS = "/api/v1/label-templates/sheets"


@pytest.fixture
async def printer_of_labels(db_session) -> dict[str, str]:
    """Somebody who may read the inventory — and so print its labels — and nothing of the label editor."""
    group = Group(name="Shelf staff", permissions=["inventory:read"])
    user = User(username="shelf", password_hash=get_password_hash("TestPass123!"), role="user")
    db_session.add_all([group, user])
    await db_session.commit()
    await db_session.refresh(group)
    await db_session.refresh(user)
    await db_session.execute(
        text("INSERT INTO user_groups (user_id, group_id) VALUES (:uid, :gid)"), {"uid": user.id, "gid": group.id}
    )
    await db_session.commit()
    return {"Authorization": f"Bearer {await mint_test_access(db_session, user=user)}"}


@pytest.fixture
async def a_catalogue(db_session) -> dict[str, int]:
    design = LabelTemplate(**BUILTIN_TEMPLATES[2])
    sheet = LabelSheet(**BUILTIN_SHEETS[0])
    spool = Spool(material="PLA", subtype="Basic", brand="Polymaker", color_name="Ivory", rgba="F5E6D3FF")
    db_session.add_all([design, sheet, spool])
    await db_session.commit()
    return {"design": design.id, "sheet": sheet.id, "spool": spool.id}


async def test_they_can_print(async_client: AsyncClient, printer_of_labels, a_catalogue):
    printed = await async_client.post(
        "/api/v1/inventory/labels",
        json={"spools": [{"id": a_catalogue["spool"]}], "template_id": a_catalogue["design"]},
        headers=printer_of_labels,
    )

    assert printed.status_code == 200, printed.text


async def test_so_they_see_the_designs_and_the_sheets(async_client: AsyncClient, printer_of_labels, a_catalogue):
    designs = await async_client.get(TEMPLATES, headers=printer_of_labels)
    sheets = await async_client.get(SHEETS, headers=printer_of_labels)

    assert designs.status_code == 200, designs.text
    assert [row["id"] for row in designs.json()] == [a_catalogue["design"]]
    assert sheets.status_code == 200, sheets.text
    assert [row["id"] for row in sheets.json()] == [a_catalogue["sheet"]]


async def test_the_editor_s_doors_stay_shut(async_client: AsyncClient, printer_of_labels, a_catalogue):
    design = {"name": "Mine", "width_mm": 40.0, "height_mm": 20.0, "elements": []}
    sheet = {"name": "Mine", "page_size": "A4", "cell_width_mm": 60.0, "cell_height_mm": 30.0, "cols": 3, "rows": 7}

    refused = [
        await async_client.post(TEMPLATES, json=design, headers=printer_of_labels),
        await async_client.put(f"{TEMPLATES}/{a_catalogue['design']}", json=design, headers=printer_of_labels),
        await async_client.delete(f"{TEMPLATES}/{a_catalogue['design']}", headers=printer_of_labels),
        await async_client.post(f"{TEMPLATES}/{a_catalogue['design']}/duplicate", headers=printer_of_labels),
        await async_client.post(SHEETS, json=sheet, headers=printer_of_labels),
        await async_client.put(f"{SHEETS}/{a_catalogue['sheet']}", json=sheet, headers=printer_of_labels),
        await async_client.delete(f"{SHEETS}/{a_catalogue['sheet']}", headers=printer_of_labels),
        # The editor's own reads and renders: an unsaved design drawn on demand.
        await async_client.post(f"{TEMPLATES}/preview", json={"template": design}, headers=printer_of_labels),
        await async_client.get(f"{TEMPLATES}/placeholders", headers=printer_of_labels),
    ]

    assert [response.status_code for response in refused] == [403] * len(refused)


async def test_someone_with_neither_right_sees_nothing(async_client: AsyncClient, db_session, a_catalogue):
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

    assert (await async_client.get(TEMPLATES, headers=headers)).status_code == 403
    assert (await async_client.get(SHEETS, headers=headers)).status_code == 403
