"""A spool is named with its subtype by default, and no behaviour hangs on the name template (audit D3).

Upstream 87e0a4c3 fixed one screen that built a spool's name without its
subtype. Every screen of ours names a spool through ONE user template, so the
real gap was the template's default — "{brand} {material} {color_name}" — which
named PLA, PLA Matte and PLA Wood of one colour identically everywhere.

⚠️ The template is the operator's to change, so nothing but DISPLAY may depend on
it (owner, 2026-09-26). Two places did: the assignment picker searched only the
composed name, so what it could find depended on the template; and a label
printed without the setting took a name of its own instead of the list's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool

pytestmark = pytest.mark.integration

DEFAULT = "{brand} {material} {subtype} {color_name}"


def test_the_default_template_names_the_subtype():
    from backend.app.schemas.settings import AppSettings
    from backend.app.services.inventory_service import DEFAULT_SPOOL_DISPLAY_TEMPLATE

    assert DEFAULT_SPOOL_DISPLAY_TEMPLATE == DEFAULT
    assert AppSettings.model_fields["spool_display_template"].default == DEFAULT


@pytest.mark.asyncio
async def test_a_label_without_the_setting_reads_like_the_list(db_session):
    from backend.app.api.routes.labels import _apply_naming_template
    from backend.app.services.inventory_service import spool_display_template
    from backend.app.services.label_context import spool_context

    spool = Spool(id=1, brand="SUNLU", material="PETG", subtype="Basic", color_name="Black", label_weight=1000)
    context = _apply_naming_template(
        spool_context(spool, deeplink_base="http://x"), await spool_display_template(db_session)
    )

    assert context["display_name"] == "SUNLU PETG Basic Black"


def test_no_route_reads_the_naming_setting_raw():
    """``inventory_service.spool_display_template`` is the one reader — it is
    what supplies the default when the operator never set one."""
    routes = Path(__file__).resolve().parents[2] / "app" / "api" / "routes"
    raw = re.compile(r"""get_setting\(\s*db\s*,\s*["']spool_display_template["']""")
    offenders = [p.name for p in routes.glob("*.py") if raw.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


# -- the assignment picker finds what the fields say, whatever the template ----

PICKER = "/api/v1/inventory/spools/picker"
PICKER_PARAMS = {"printer_id": 1, "ams_id": 0, "tray_id": 0, "show_all": "true"}


async def _picker_case(db):
    db.add(Settings(key="spool_display_template", value="{brand} {color_name}"))
    matte = Spool(material="PLA", subtype="Matte", brand="Acme", color_name="Red", note="shelf 4", label_weight=1000)
    plain = Spool(material="PLA", brand="Acme", color_name="Red", label_weight=1000)
    db.add_all([matte, plain])
    await db.commit()
    return matte, plain


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["Matte", "shelf", "PLA Matte"])
async def test_the_picker_searches_the_fields_not_only_the_composed_name(async_client, db_session, q):
    matte, _plain = await _picker_case(db_session)
    response = await async_client.get(PICKER, params={**PICKER_PARAMS, "q": q})

    assert response.status_code == 200, response.text
    assert [s["id"] for s in response.json()["items"]] == [matte.id]


@pytest.mark.asyncio
async def test_the_picker_still_matches_across_the_composed_name(async_client, db_session):
    """'Acme Red' spans two fields — only the composed name holds it."""
    matte, plain = await _picker_case(db_session)
    response = await async_client.get(PICKER, params={**PICKER_PARAMS, "q": "cme Re"})

    assert sorted(s["id"] for s in response.json()["items"]) == sorted([matte.id, plain.id])
