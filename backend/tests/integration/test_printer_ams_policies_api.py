"""Per-printer AMS policies travel as one namespaced JSON object."""

import pytest
from sqlalchemy import select

from backend.app.models.printer import Printer

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_fresh_printer_reports_default_off_policy(async_client, printer_factory):
    printer = await printer_factory()
    body = (await async_client.get(f"/api/v1/printers/{printer.id}")).json()
    assert body["ams_policies"] == {
        "backup_compatibility": {
            "normalize_color": False,
            "canonical_color_rgba": "000000FF",
            "generic_base_material": False,
        }
    }


async def test_patch_writes_namespace_and_keeps_foreign_keys(async_client, db_session, printer_factory):
    printer_id = (await printer_factory(ams_policies={"future_policy": {"x": 1}})).id
    resp = await async_client.patch(
        f"/api/v1/printers/{printer_id}",
        json={"ams_policies": {"backup_compatibility": {"normalize_color": True, "canonical_color_rgba": "1a1a1aff"}}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ams_policies"]["backup_compatibility"] == {
        "normalize_color": True,
        "canonical_color_rgba": "1A1A1AFF",
        "generic_base_material": False,
    }
    db_session.expire_all()  # the id is read above, before the instance is expired
    row = (await db_session.execute(select(Printer).where(Printer.id == printer_id))).scalar_one()
    assert row.ams_policies["future_policy"] == {"x": 1}  # foreign namespace survived the merge


async def test_translucent_canonical_color_is_refused(async_client, printer_factory):
    printer = await printer_factory()
    resp = await async_client.patch(
        f"/api/v1/printers/{printer.id}",
        json={"ams_policies": {"backup_compatibility": {"canonical_color_rgba": "00000080"}}},
    )
    assert resp.status_code == 422
