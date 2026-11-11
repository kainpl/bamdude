"""Opus BA3: historical rows remain operable without weakening new edits."""

from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from sqlalchemy import select

from backend.app.core.credential_config import CONFIG_GUARD_KEY
from backend.app.models.camera import Camera
from backend.app.models.notification import NotificationProvider
from backend.app.models.settings import Settings
from backend.app.services import log_reader
from backend.app.services.rest_smart_plug import rest_smart_plug_service

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "values,credential",
    [
        ({"ip_address": "tasmota-plug.local", "password": "OLD_TASMOTA_SECRET"}, "password"),
        ({"plug_type": "homeassistant", "ha_entity_id": "fan.bedroom", "ha_power_entity": "legacy.watts"}, None),
        (
            {
                "plug_type": "rest",
                "rest_on_url": "https://bridge.example/" + "x" * 540,
                "rest_headers": '{"Authorization":"OLD_REST_SECRET"}',
            },
            "rest_headers",
        ),
    ],
)
async def test_legacy_plug_can_rename_disable_and_clear(async_client, smart_plug_factory, values, credential):
    plug = await smart_plug_factory(**values)
    path = f"/api/v1/smart-plugs/{plug.id}"
    for endpoint in (path, "/api/v1/smart-plugs/"):
        response = await async_client.get(endpoint)
        assert response.status_code == 200, response.text
        assert "OLD_" not in response.text
    # An existing editor may resubmit an identical legacy destination.
    fields = {k: v for k, v in values.items() if k not in ("password", "rest_headers", "rest_on_url")}
    response = await async_client.patch(path, json={"name": "Renamed", **fields})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    response = await async_client.patch(path, json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    if credential:
        response = await async_client.patch(path, json={"secret_actions": {credential: "clear"}})
        assert response.status_code == 200, response.text
        assert response.json()["configured_secrets"][credential] is False
    if values.get("plug_type") == "rest":
        response = await async_client.patch(
            path, json={"secret_actions": {"rest_on_url": "clear", "rest_off_url": "clear"}}
        )
        assert response.status_code == 200, response.text
        assert response.json()["configured_secrets"]["rest_on_url"] is False


@pytest.mark.parametrize(
    "initial,change",
    [
        ({"ip_address": "old.local"}, {"ip_address": "new.local"}),
        ({"plug_type": "homeassistant", "ha_entity_id": "fan.old"}, {"ha_entity_id": "fan.new"}),
        ({"plug_type": "homeassistant", "ha_entity_id": "fan.old"}, {"ha_power_entity": "legacy.new"}),
        ({"plug_type": "rest"}, {"rest_on_url": "https://bridge.example/" + "n" * 540}),
        ({}, {"name": "n" * 101}),
        ({}, {"ip_address": None}),
        ({"plug_type": "homeassistant"}, {"ha_entity_id": None}),
        ({"plug_type": "rest", "rest_off_url": None}, {"secret_actions": {"rest_on_url": "clear"}}),
    ],
)
async def test_new_invalid_or_missing_required_value_is_atomic(async_client, smart_plug_factory, initial, change):
    plug = await smart_plug_factory(**initial)
    path = f"/api/v1/smart-plugs/{plug.id}"
    before = (await async_client.get(path)).json()
    response = await async_client.patch(path, json={"auto_on": False, **change})
    assert response.status_code == 422, response.text
    assert (await async_client.get(path)).json() == before


async def test_legacy_rest_draft_keeps_scope_and_releases_transaction(async_client, smart_plug_factory, monkeypatch):
    plug = await smart_plug_factory(
        plug_type="rest",
        rest_on_url="https://bridge.example/" + "x" * 540,
        rest_headers='{"Authorization":"OLD_HEADER_SECRET"}',
    )
    from backend.app.api.routes import smart_plugs

    sessions = []
    lock = smart_plugs.lock_credential_config

    async def record(db):
        sessions.append(db)
        await lock(db)

    async def outbound(*args, **kwargs):
        assert not sessions[-1].in_transaction()
        return {"success": True}

    monkeypatch.setattr(smart_plugs, "lock_credential_config", record)
    network = AsyncMock(side_effect=outbound)
    monkeypatch.setattr(rest_smart_plug_service, "test_connection", network)
    response = await async_client.post(
        "/api/v1/smart-plugs/rest/test-connection",
        json={
            "plug_id": plug.id,
            "draft": {"name": "Unsaved"},
        },
    )
    assert response.status_code == 200, response.text
    assert network.await_args.args[0] == plug.rest_on_url
    network.reset_mock()
    response = await async_client.post(
        "/api/v1/smart-plugs/rest/test-connection",
        json={
            "plug_id": plug.id,
            "draft": {"rest_on_url": "https://other.example/new"},
        },
    )
    assert response.status_code == 400
    network.assert_not_awaited()
    assert (await async_client.get(f"/api/v1/smart-plugs/{plug.id}")).json()["name"] == plug.name


@pytest.mark.parametrize("server", [None, "https://notify.example/prefix"])
async def test_provider_collector_masks_default_ntfy_topic_and_header(db_session, server):
    import json

    config = {"topic": "ab", "auth_header": "HEADER_CAPABILITY"}
    if server is not None:
        config["server"] = server
    db_session.add(NotificationProvider(name="ntfy", provider_type="ntfy", config=json.dumps(config)))
    await db_session.commit()
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    url = (server or "https://ntfy.sh") + "/ab"
    assert values["ab"] == "[REDACTED]"
    assert values["HEADER_CAPABILITY"] == "[REDACTED]"
    assert values[url] == "[REDACTED]"
    assert log_reader.sanitize_log_content(url + " HEADER_CAPABILITY ab", values) == "[REDACTED] [REDACTED] [REDACTED]"


async def test_collector_excludes_only_anchor_not_real_short_credentials(db_session):
    assert await db_session.scalar(select(Settings.value).where(Settings.key == CONFIG_GUARD_KEY)) == "1"
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    assert "1" not in values
    diagnostic = "2026-11-10 12:00:00,000 useful diagnostic OID 1.3.6.1.4.1"
    assert log_reader.sanitize_log_content(diagnostic, values) == diagnostic
    db_session.add(Settings(key="future_password", value="1"))
    await db_session.commit()
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    assert values["1"] == "[REDACTED]"
    assert log_reader.sanitize_log_content("peer echoed 1", values) == "peer echoed [REDACTED]"


@pytest.mark.parametrize("door", ["logs", "logs/download", "log-archives/bamdude-2026-11-09.log/download"])
async def test_standalone_camera_capabilities_collected_and_hidden_in_support(
    async_client,
    db_session,
    tmp_path,
    monkeypatch,
    door,
):
    from backend.app.api.routes import support

    stream = "rtsp://user:STREAM_PASS_CANARY@camera.example/STREAM_PATH_CANARY"
    snapshot = "https://camera.example/SNAPSHOT_PATH_CANARY?token=SNAPSHOT_QUERY_CANARY"
    db_session.add(Camera(name="Room", camera_type="rtsp", url=stream, snapshot_url=snapshot, enabled=False))
    await db_session.commit()
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    assert values[stream] == values[snapshot] == values["STREAM_PASS_CANARY"] == "[REDACTED]"
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    content = "".join(
        "2026-11-10 12:00:00,000 ERROR [test.camera] peer echoed " + spelling + "\n"
        for value in (stream, snapshot)
        for spelling in (value, quote(value, safe=""), quote(quote(value, safe=""), safe=""))
    )
    for filename in ("bamdude.log", "bamdude-2026-11-09.log"):
        (tmp_path / filename).write_text(content, encoding="utf-8")
    response = await async_client.get("/api/v1/support/" + door)
    assert response.status_code == 200, response.text
    assert "CANARY" not in response.text and "[REDACTED]" in response.text
