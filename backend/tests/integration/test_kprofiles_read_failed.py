"""The Profiles listing says when the table was not read (upstream 080d866fb).

An unanswered ``extrusion_cali_get`` used to be a 200 with ``profiles: []`` —
the page offered "Create first profile" over profiles that exist, and the
Configure Slot dialog, finding nothing to pre-select, saved ``cali_idx -1``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import KProfileReadError


def _manager(client) -> MagicMock:
    manager = MagicMock()
    manager.get_client.return_value = client
    manager.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
    return manager


@pytest.mark.asyncio
async def test_an_unanswered_read_is_a_504_with_a_code(async_client, printer_factory):
    printer = await printer_factory(model="P1S")
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = []
    client.get_kprofiles = AsyncMock(side_effect=KProfileReadError("no answer"))

    with (
        patch("backend.app.api.routes.kprofiles.printer_manager", _manager(client)),
        patch("backend.app.services.calibration_service.printer_manager", _manager(client)),
    ):
        response = await async_client.get(f"/api/v1/printers/{printer.id}/kprofiles/?nozzle_diameter=0.4")

    assert response.status_code == 504, response.text
    assert response.json()["detail"]["error"] == "kprofiles_unread"


@pytest.mark.asyncio
async def test_an_answered_empty_table_is_still_a_200(async_client, printer_factory):
    printer = await printer_factory(model="P1S")
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = []
    client.get_kprofiles = AsyncMock(return_value=[])

    with (
        patch("backend.app.api.routes.kprofiles.printer_manager", _manager(client)),
        patch("backend.app.services.calibration_service.printer_manager", _manager(client)),
    ):
        response = await async_client.get(f"/api/v1/printers/{printer.id}/kprofiles/?nozzle_diameter=0.4")

    assert response.status_code == 200, response.text
    assert response.json()["profiles"] == []
