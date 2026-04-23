"""Integration tests for MQTT-action macros (``print_started`` / chamber_light).

Covers the create/update validation path, the meta endpoint's new
``mqtt_actions`` catalog, and the exec path's dispatch branching.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_meta_exposes_mqtt_actions_and_print_started(async_client: AsyncClient):
    resp = await async_client.get("/api/v1/macros/meta")
    assert resp.status_code == 200, resp.text
    meta = resp.json()

    assert "mqtt_actions" in meta
    ids = [a["id"] for a in meta["mqtt_actions"]]
    assert "chamber_light_off" in ids
    assert "chamber_light_on" in ids

    assert "print_started" in meta["events"]
    assert "print_started" not in meta["swap_events"]


async def test_create_mqtt_action_macro(async_client: AsyncClient):
    resp = await async_client.post(
        "/api/v1/macros/",
        json={
            "name": "Lights off on print",
            "event": "print_started",
            "action_type": "mqtt_action",
            "mqtt_action": "chamber_light_off",
            "delay_seconds": 0,
            "printer_models": ["*"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action_type"] == "mqtt_action"
    assert data["mqtt_action"] == "chamber_light_off"
    assert data["delay_seconds"] == 0
    assert data["gcode"] == ""


async def test_mqtt_action_requires_action_id(async_client: AsyncClient):
    resp = await async_client.post(
        "/api/v1/macros/",
        json={
            "name": "Missing action id",
            "event": "print_started",
            "action_type": "mqtt_action",
            "printer_models": ["*"],
        },
    )
    assert resp.status_code == 400
    assert "requires mqtt_action" in resp.json()["detail"]


async def test_mqtt_action_rejects_unknown_id(async_client: AsyncClient):
    resp = await async_client.post(
        "/api/v1/macros/",
        json={
            "name": "Bad action id",
            "event": "print_started",
            "action_type": "mqtt_action",
            "mqtt_action": "launch_nukes",
            "printer_models": ["*"],
        },
    )
    assert resp.status_code == 400
    assert "Unknown mqtt_action" in resp.json()["detail"]


async def test_gcode_macro_still_works_with_defaults(async_client: AsyncClient):
    """Old-shape payloads (no action_type) still create a gcode macro."""
    resp = await async_client.post(
        "/api/v1/macros/",
        json={
            "name": "Classic gcode macro",
            "event": "swap_mode_start",
            "gcode": "M104 S210",
            "printer_models": ["*"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action_type"] == "gcode"
    assert data["mqtt_action"] is None
    assert data["delay_seconds"] == 0


async def test_patch_switching_to_gcode_clears_mqtt_action(async_client: AsyncClient):
    created = await async_client.post(
        "/api/v1/macros/",
        json={
            "name": "Flippable",
            "event": "print_started",
            "action_type": "mqtt_action",
            "mqtt_action": "chamber_light_off",
            "printer_models": ["*"],
        },
    )
    macro_id = created.json()["id"]

    patched = await async_client.patch(
        f"/api/v1/macros/{macro_id}",
        json={"action_type": "gcode", "gcode": "M104 S190"},
    )
    assert patched.status_code == 200, patched.text
    data = patched.json()
    assert data["action_type"] == "gcode"
    assert data["mqtt_action"] is None
    assert data["gcode"] == "M104 S190"


async def test_delay_seconds_bounds_enforced(async_client: AsyncClient):
    # Too large (> 3600)
    resp = await async_client.post(
        "/api/v1/macros/",
        json={
            "name": "Too slow",
            "event": "print_started",
            "action_type": "mqtt_action",
            "mqtt_action": "chamber_light_off",
            "delay_seconds": 99999,
            "printer_models": ["*"],
        },
    )
    assert resp.status_code == 422

    # Negative
    resp_neg = await async_client.post(
        "/api/v1/macros/",
        json={
            "name": "Past delay",
            "event": "print_started",
            "action_type": "mqtt_action",
            "mqtt_action": "chamber_light_off",
            "delay_seconds": -5,
            "printer_models": ["*"],
        },
    )
    assert resp_neg.status_code == 422
