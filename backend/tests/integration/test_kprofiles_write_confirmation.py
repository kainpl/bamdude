"""A K-profile write the printer did not answer is not reported as confirmed (upstream f45cc599f).

Silence stays a success — firmware that never answers must not fail every
save — but the answer and the audit row now say that nothing confirmed it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.calibration_audit import CalibrationAudit

_PROFILE = {
    "slot_id": 0,
    "extruder_id": 0,
    "nozzle_id": "HS00-0.4",
    "nozzle_diameter": "0.4",
    "filament_id": "GFG99",
    "name": "Generic PETG K0.032",
    "k_value": "0.032000",
    "n_coef": "0.000000",
    "ams_id": 0,
    "tray_id": -1,
    "setting_id": None,
}


def _manager(verdict) -> MagicMock:
    client = MagicMock()
    client.state.connected = True
    client.set_kprofile = MagicMock(return_value="41")
    client.set_kprofiles_batch = MagicMock(return_value="42")
    client.delete_kprofile = MagicMock(return_value="43")
    client.await_cali_verdict = AsyncMock(return_value=verdict)
    manager = MagicMock()
    manager.get_client.return_value = client
    manager.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
    return manager


async def _audit_results(db_session) -> list[tuple[str, str]]:
    rows = (await db_session.execute(select(CalibrationAudit).order_by(CalibrationAudit.id))).scalars().all()
    return [(row.action, row.result) for row in rows]


@pytest.mark.asyncio
async def test_an_unanswered_add_says_unconfirmed_in_the_answer_and_the_audit(
    async_client, db_session, printer_factory
):
    printer = await printer_factory(model="P1S")
    with patch("backend.app.api.routes.kprofiles.printer_manager", _manager((True, "", False))):
        response = await async_client.post(f"/api/v1/printers/{printer.id}/kprofiles/", json=_PROFILE)

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert response.json()["confirmed"] is False
    assert await _audit_results(db_session) == [("kprofile_add", "unconfirmed")]


@pytest.mark.asyncio
async def test_an_answered_add_is_confirmed(async_client, db_session, printer_factory):
    printer = await printer_factory(model="P1S")
    with patch("backend.app.api.routes.kprofiles.printer_manager", _manager((True, "", True))):
        response = await async_client.post(f"/api/v1/printers/{printer.id}/kprofiles/", json=_PROFILE)

    assert response.status_code == 200, response.text
    assert response.json()["confirmed"] is True
    assert await _audit_results(db_session) == [("kprofile_add", "ok")]


@pytest.mark.asyncio
async def test_a_refusal_is_still_an_error(async_client, db_session, printer_factory):
    printer = await printer_factory(model="P1S")
    with patch("backend.app.api.routes.kprofiles.printer_manager", _manager((False, "busy", True))):
        response = await async_client.post(f"/api/v1/printers/{printer.id}/kprofiles/", json=_PROFILE)

    assert response.status_code == 500
    assert await _audit_results(db_session) == [("kprofile_add", "error")]


@pytest.mark.asyncio
async def test_an_unanswered_batch_and_delete_say_unconfirmed(async_client, db_session, printer_factory):
    printer = await printer_factory(model="P1S")
    with patch("backend.app.api.routes.kprofiles.printer_manager", _manager((True, "", False))):
        batch = await async_client.post(f"/api/v1/printers/{printer.id}/kprofiles/batch", json=[_PROFILE])
        delete = await async_client.request(
            "DELETE",
            f"/api/v1/printers/{printer.id}/kprofiles/",
            json={
                "slot_id": 3,
                "extruder_id": 0,
                "nozzle_id": "HS00-0.4",
                "nozzle_diameter": "0.4",
                "filament_id": "GFG99",
                "setting_id": None,
            },
        )

    assert batch.status_code == 200, batch.text
    assert batch.json()["confirmed"] is False
    assert delete.status_code == 200, delete.text
    assert delete.json()["confirmed"] is False
    assert await _audit_results(db_session) == [
        ("kprofile_batch_add", "unconfirmed"),
        ("kprofile_delete", "unconfirmed"),
    ]


@pytest.mark.asyncio
async def test_a_printer_that_never_answers_such_writes_keeps_its_quiet_success(
    async_client, db_session, printer_factory
):
    """Silence from a connection that has never answered a K write says nothing: saved, as before."""
    printer = await printer_factory(model="P1S")
    manager = _manager((True, "", False))
    manager.get_client.return_value.answers_cali_writes = False
    with patch("backend.app.api.routes.kprofiles.printer_manager", manager):
        add = await async_client.post(f"/api/v1/printers/{printer.id}/kprofiles/", json=_PROFILE)
        batch = await async_client.post(f"/api/v1/printers/{printer.id}/kprofiles/batch", json=[_PROFILE])
        delete = await async_client.request(
            "DELETE",
            f"/api/v1/printers/{printer.id}/kprofiles/",
            json={
                "slot_id": 3,
                "extruder_id": 0,
                "nozzle_id": "HS00-0.4",
                "nozzle_diameter": "0.4",
                "filament_id": "GFG99",
                "setting_id": None,
            },
        )

    assert [r.json()["confirmed"] for r in (add, batch, delete)] == [True, True, True]
    assert await _audit_results(db_session) == [
        ("kprofile_add", "ok"),
        ("kprofile_batch_add", "ok"),
        ("kprofile_delete", "ok"),
    ]


@pytest.mark.asyncio
async def test_a_refusal_from_such_a_printer_is_still_an_error(async_client, db_session, printer_factory):
    printer = await printer_factory(model="P1S")
    manager = _manager((False, "busy", True))
    manager.get_client.return_value.answers_cali_writes = False
    with patch("backend.app.api.routes.kprofiles.printer_manager", manager):
        response = await async_client.post(f"/api/v1/printers/{printer.id}/kprofiles/", json=_PROFILE)

    assert response.status_code == 500
    assert await _audit_results(db_session) == [("kprofile_add", "error")]
