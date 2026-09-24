"""New API keys carry BamDude's own prefix; keys issued with Bambuddy's keep working.

Keys were generated as ``bb_…`` — Bambuddy's prefix, inherited with the fork.
New keys are ``bd_…``. Only a key's hash is stored and the key itself is shown
once, so a key already sitting in Home Assistant, Node-RED or the label bridge
cannot be re-issued under the new prefix: ``bb_`` stays accepted for good.

The prefix matters only on ``Authorization: Bearer`` — it is how a key is told
from a session JWT, by the auth middleware and by every gate. ``X-API-Key``
never looked at it.
"""

from __future__ import annotations

import pytest

LEGACY_KEY = "bb_" + "L" * 43


@pytest.fixture
async def legacy_key(db_session):
    """A key as a Bambuddy-era install holds it: ``bb_…``, ownerless, read scope."""
    from backend.app.core.auth import get_password_hash
    from backend.app.models.api_key import APIKey

    db_session.add(
        APIKey(
            name="from the Bambuddy days",
            key_hash=get_password_hash(LEGACY_KEY),
            key_prefix=LEGACY_KEY[:8] + "...",
            user_id=None,
            can_read_status=True,
        )
    )
    await db_session.commit()
    return LEGACY_KEY


async def _new_key(async_client) -> dict:
    response = await async_client.post("/api/v1/api-keys/", json={"name": "fresh"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_new_key_carries_bamdudes_prefix(async_client):
    body = await _new_key(async_client)

    assert body["key"].startswith("bd_")
    assert body["key_prefix"].startswith("bd_")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_new_key_works_as_a_bearer_token(async_client):
    key = (await _new_key(async_client))["key"]
    bearer = {"Authorization": f"Bearer {key}"}

    printers = await async_client.get("/api/v1/printers/", headers=bearer)
    me = await async_client.get("/api/v1/auth/me", headers=bearer)

    assert (printers.status_code, me.status_code) == (200, 200), (printers.text, me.text)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("transport", ["x-api-key", "bearer"])
async def test_a_bambuddy_era_key_still_works(async_client, legacy_key, transport):
    headers = {"X-API-Key": legacy_key} if transport == "x-api-key" else {"Authorization": f"Bearer {legacy_key}"}

    response = await async_client.get("/api/v1/printers/", headers=headers)

    assert response.status_code == 200, response.text
