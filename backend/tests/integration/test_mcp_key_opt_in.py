"""MCP opt-ins are independent of REST scopes and require an active owner."""

from dataclasses import fields

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import auth
from backend.app.models.api_key import APIKey


async def test_existing_rest_key_has_no_mcp_grant(async_client):
    response = await async_client.post("/api/v1/api-keys/", json={"name": "existing integration"})
    assert response.status_code == 200
    body = response.json()
    assert body["can_access_mcp"] is False
    assert body["can_read_mcp_logs"] is False
    authority = await auth.resolve_api_key_authority(None, body["key"])
    assert authority.mcp_allowed is False
    assert authority.mcp_logs_allowed is False
    assert "mcp_allowed" in {field.name for field in fields(authority)}


async def test_mcp_grants_roundtrip_and_revoke_in_fresh_authority(async_client):
    created = await async_client.post(
        "/api/v1/api-keys/",
        json={"name": "assistant", "can_access_mcp": True, "can_read_mcp_logs": True},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["can_access_mcp"] and body["can_read_mcp_logs"]
    before = await auth.resolve_api_key_authority(None, body["key"])
    assert before.mcp_allowed and before.mcp_logs_allowed
    assert "can_access_mcp" not in before.scope_flags
    changed = await async_client.patch(f"/api/v1/api-keys/{body['id']}", json={"can_access_mcp": False})
    assert changed.status_code == 200, changed.text
    assert changed.json()["can_read_mcp_logs"] is False
    after = await auth.resolve_api_key_authority(None, body["key"])
    assert not after.mcp_allowed and not after.mcp_logs_allowed
    assert before.mcp_allowed  # the earlier request's immutable snapshot


async def test_log_opt_in_requires_mcp(async_client):
    response = await async_client.post("/api/v1/api-keys/", json={"name": "invalid", "can_read_mcp_logs": True})
    assert response.status_code == 400, response.text
    created = await async_client.post("/api/v1/api-keys/", json={"name": "plain"})
    response = await async_client.patch(f"/api/v1/api-keys/{created.json()['id']}", json={"can_read_mcp_logs": True})
    assert response.status_code == 400, response.text


async def test_ownerless_legacy_key_cannot_be_promoted_to_mcp(async_client, db_session):
    key = APIKey(name="legacy", key_prefix="bd_test", key_hash="unused", user_id=None)
    db_session.add(key)
    await db_session.commit()
    response = await async_client.patch(f"/api/v1/api-keys/{key.id}", json={"can_access_mcp": True})
    assert response.status_code == 400, response.text
    await db_session.refresh(key)
    assert not key.can_access_mcp


async def test_migration_preserves_rest_scopes_and_never_grants_old_keys(tmp_path):
    from backend.app.migrations import m212_mcp_key_opt_in as migration

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE api_keys (id INTEGER PRIMARY KEY, can_read_status BOOLEAN)"))
            await conn.execute(text("INSERT INTO api_keys VALUES (1, 1), (2, 0)"))
            await migration.upgrade(conn)
            await migration.upgrade(conn)
            rows = (
                await conn.execute(
                    text("SELECT can_read_status, can_access_mcp, can_read_mcp_logs FROM api_keys ORDER BY id")
                )
            ).all()
            assert rows == [(1, 0, 0), (0, 0, 0)]
            await conn.execute(text("INSERT INTO api_keys (id) VALUES (3)"))
            row = (await conn.execute(text("SELECT can_access_mcp, can_read_mcp_logs FROM api_keys WHERE id=3"))).one()
            assert row == (0, 0)
    finally:
        await engine.dispose()
