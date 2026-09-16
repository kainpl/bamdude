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


async def test_create_persists_a_namespace_the_form_sent(async_client):
    """The frontend's ``PrinterCreate`` has carried ``ams_policies`` since the
    policy shipped; without the field on the backend shape a create that sent
    one was accepted and silently dropped, and the switch came back off."""
    from unittest.mock import AsyncMock, patch

    with patch("backend.app.api.routes.printers.printer_manager") as pm:
        pm.test_connection = AsyncMock(return_value={"success": True})
        pm.connect_printer = AsyncMock()
        created = await async_client.post(
            "/api/v1/printers/",
            json={
                "name": "Policy at birth",
                "serial_number": "00M09A700000001",
                "ip_address": "192.168.1.77",
                "access_code": "12345678",
                "model": "P1S",
                "ams_policies": {
                    "backup_compatibility": {"normalize_color": True, "canonical_color_rgba": "#1a1a1aff"}
                },
            },
        )
    assert created.status_code == 200, created.text
    body = (await async_client.get(f"/api/v1/printers/{created.json()['id']}")).json()
    assert body["ams_policies"]["backup_compatibility"] == {
        "normalize_color": True,
        "canonical_color_rgba": "1A1A1AFF",  # normalised on the way in
        "generic_base_material": False,
    }


async def test_create_without_the_namespace_keeps_the_column_default(async_client):
    from unittest.mock import AsyncMock, patch

    with patch("backend.app.api.routes.printers.printer_manager") as pm:
        pm.test_connection = AsyncMock(return_value={"success": True})
        pm.connect_printer = AsyncMock()
        created = await async_client.post(
            "/api/v1/printers/",
            json={
                "name": "No policy",
                "serial_number": "00M09A700000002",
                "ip_address": "192.168.1.78",
                "access_code": "12345678",
                "model": "P1S",
            },
        )
    assert created.status_code == 200, created.text
    assert created.json()["ams_policies"]["backup_compatibility"]["normalize_color"] is False


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
    # The form has to be able to point at the field that was refused.
    assert "canonical_color_rgba" in resp.text


async def test_a_patch_that_says_nothing_about_the_policy_leaves_it_alone(async_client, printer_factory):
    """``exclude_unset`` is what makes this true, and it is the difference
    between an unrelated rename and a policy silently switched off."""
    printer = await printer_factory(
        ams_policies={"backup_compatibility": {"normalize_color": True, "canonical_color_rgba": "1A1A1AFF"}}
    )
    resp = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"name": "Renamed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ams_policies"]["backup_compatibility"] == {
        "normalize_color": True,
        "canonical_color_rgba": "1A1A1AFF",
        "generic_base_material": False,
    }


@pytest.mark.parametrize("sent", [None, {}])
async def test_an_empty_namespace_is_a_no_op_not_a_reset(async_client, printer_factory, sent):
    """``ams_policies: null`` is what a GET → edit → PATCH client sends for a
    field it never touched; neither it nor ``{}`` may wipe the policy."""
    printer = await printer_factory(ams_policies={"backup_compatibility": {"generic_base_material": True}})
    resp = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"ams_policies": sent})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ams_policies"]["backup_compatibility"]["generic_base_material"] is True


async def test_the_list_endpoint_carries_the_policy_too(async_client, printer_factory):
    """The Printers page reads the list, not one row at a time — a field only
    the detail route carries would leave every card's switch looking off."""
    printer = await printer_factory(ams_policies={"backup_compatibility": {"normalize_color": True}})
    rows = (await async_client.get("/api/v1/printers/")).json()
    row = next(r for r in rows if r["id"] == printer.id)
    assert row["ams_policies"]["backup_compatibility"]["normalize_color"] is True


async def test_apply_walks_the_real_service_and_publishes_the_projected_plan(async_client, db_session, printer_factory):
    """The route → ``bulk_apply`` wiring, with nothing between them patched.

    Every other test here stubs ``bulk_apply``, so a route that stopped passing
    the client (or stopped nudging the pushall the read-back verification waits
    for) would keep them all green."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services import ams_advertised_overlay as overlay

    printer = await printer_factory(model="P1S", ams_policies={"backup_compatibility": {"normalize_color": True}})
    spool = Spool(material="PETG", rgba="FF0000FF", filament_family_id="GFG99", label_weight=1000)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    db_session.add(
        SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0, fingerprint_type="PETG")
    )
    await db_session.commit()

    state = SimpleNamespace(
        nozzles=[],
        support_user_preset=False,
        ams_extruder_map={},
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {
                            "id": 0,
                            "tray_type": "PETG",
                            "tray_color": "FF0000FF",
                            "tray_info_idx": "GFG99",
                            "tag_uid": "0000000000000000",
                            "tray_uuid": "",
                            "state": 11,
                            "exists": True,
                        }
                    ],
                }
            ]
        },
    )
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    client.state.connected = True
    with (
        patch("backend.app.api.routes.printers.printer_manager") as route_pm,
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as walk_pm,
    ):
        route_pm.get_client.return_value = client
        route_pm.is_print_active.return_value = False
        route_pm.get_status.return_value = state
        walk_pm.get_status.return_value = state
        resp = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams-policies/backup-compatibility/apply", json={"dry_run": False}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    applied_rows = [r for r in body["rows"] if r["action"] == "apply"]
    assert len(applied_rows) == 1 and applied_rows[0]["published"] is True
    assert body["applied"] == 1 and body["dry_run"] is False
    assert client.ams_set_filament_setting.call_args.kwargs["tray_color"] == "000000FF"
    client.request_status_update.assert_called_once()
    assert overlay.entries_for(printer.id)[(0, 0)].actual_color == "FF0000FF"


async def test_bulk_apply_refuses_while_printing_and_previews_without_mqtt(async_client, printer_factory):
    from unittest.mock import AsyncMock, patch

    printer = await printer_factory(ams_policies={"backup_compatibility": {"normalize_color": True}})
    preview_outcome = {"dry_run": True, "rows": [], "applied": 0, "skipped": 0, "would_apply": 0}
    with (
        patch("backend.app.api.routes.printers.printer_manager") as pm,
        patch("backend.app.api.routes.printers.bulk_apply", new=AsyncMock(return_value=preview_outcome)),
    ):
        pm.is_print_active.return_value = True
        pm.get_client.return_value = None
        pm.get_status.return_value = object()  # live trays are there; only the client is not
        url = f"/api/v1/printers/{printer.id}/ams-policies/backup-compatibility/apply"
        preview = await async_client.post(url, json={"dry_run": True})
        assert preview.status_code == 200, preview.text
        assert preview.json()["dry_run"] is True
        busy = await async_client.post(url, json={"dry_run": False})
        assert busy.status_code == 409 and busy.json()["detail"]
        # Idle but unreachable: an apply has nothing to publish through.
        pm.is_print_active.return_value = False
        offline = await async_client.post(url, json={"dry_run": False})
        assert offline.status_code == 400 and offline.json()["detail"]
        # No state at all: even the preview would report every slot as empty.
        pm.get_status.return_value = None
        blind = await async_client.post(url, json={"dry_run": True})
        assert blind.status_code == 400 and blind.json()["detail"]
        missing = await async_client.post(
            "/api/v1/printers/999999/ams-policies/backup-compatibility/apply", json={"dry_run": True}
        )
        assert missing.status_code == 404


async def test_rest_status_carries_the_actual_spool_behind_an_advertised_tray(async_client, printer_factory):
    """The REST status payload has its OWN shaper.

    ``printer_manager.printer_state_to_dict`` (WebSocket) and
    ``routes/printers._build_printer_status`` (REST) describe the same tray and
    the frontend merges them: a field only the socket carries is replaced away
    by the next refetch, and ``buildLoadedFilaments`` falls back to the mask —
    which is exactly what the overlay exists to prevent.
    """
    from unittest.mock import patch

    from backend.app.services import ams_advertised_overlay as overlay
    from backend.app.services.ams_advertised_overlay import OverlayEntry
    from backend.app.services.bambu_mqtt import PrinterState

    printer = await printer_factory()
    state = PrinterState()
    state.connected = True
    state.raw_data = {
        "ams": [
            {
                "id": 0,
                "tray": [
                    # Advertised: the canonical black generic we published.
                    {"id": 1, "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": "GFG99"},
                    # Nothing was ever masked here.
                    {"id": 2, "tray_type": "PLA", "tray_color": "00FF00FF", "tray_info_idx": "GFL99"},
                ],
            }
        ],
        "vt_tray": [{"id": 254, "tray_type": "PLA", "tray_color": "0000FFFF"}],
    }
    overlay.replace_printer(
        printer.id,
        {(0, 1): OverlayEntry("PETG", "FF0000FF", "GFG02", ("FF0000FF",), "000000FF", "GFG99", "internal")},
    )
    with patch("backend.app.api.routes.printers.printer_manager") as pm:
        pm.get_status.return_value = state
        pm.is_awaiting_plate_clear.return_value = False
        pm.get_drying_targets.return_value = {}
        response = await async_client.get(f"/api/v1/printers/{printer.id}/status")
    assert response.status_code == 200, response.text
    body = response.json()
    masked, plain = body["ams"][0]["tray"]
    # The live fields stay the printer's own words.
    assert (masked["tray_color"], masked["tray_info_idx"]) == ("000000FF", "GFG99")
    assert masked["actual"] == {
        "tray_color": "FF0000FF",
        "tray_type": "PETG",
        "tray_info_idx": "GFG02",
        "cols": ["FF0000FF"],
    }
    assert plain["actual"] is None
    assert all(t["actual"] is None for t in body["vt_tray"])  # external slots are never projected
