"""B credential anchor lifecycle and disjoint writers on native PostgreSQL."""

import asyncio

import pytest
from sqlalchemy import select

from backend.app.models.settings import Settings
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.integration.test_g01_b_notification_secrets import (
    CASES,
    no_bot_network,  # noqa: F401
    test_all_types_read_safe_and_runtime_config_retained as assert_provider_contract,
    test_scope_change_refused_before_save_or_draft_outbound as assert_scope_refusal,
)
from backend.tests.unit.test_credential_config import assert_anchor_lifecycle_and_serialization

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
async def credential_bootstrap(test_engine, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.core.credential_config import initialize_credential_config_anchor

    # Match runtime dialect resolution to this owned disposable engine. Patch
    # settings rather than replacing helpers: lazily imported modules must not
    # retain a synthetic function after this test finishes.
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))

    async with test_engine.begin() as conn:
        await initialize_credential_config_anchor(conn)


async def test_credential_anchor_on_postgres(test_engine):
    await assert_anchor_lifecycle_and_serialization(test_engine)


async def test_ten_provider_dtos_edits_and_drafts_on_postgres(
    async_client, notification_provider_factory, db_session, monkeypatch
):
    for kind in CASES:
        await assert_provider_contract(
            kind, "admin", async_client, notification_provider_factory, db_session, monkeypatch
        )
    for kind, change in (
        ("callmebot", {"phone": "+15550000000"}),
        ("ntfy", {"server": "https://attacker.invalid"}),
        ("ntfy", {"topic": "changed-capability"}),
        ("bark", {"server": "https://attacker.invalid"}),
        ("email", {"smtp_server": "attacker.invalid"}),
        ("webhook", {"webhook_url": "https://attacker.invalid"}),
        ("signal", {"server": "https://attacker.invalid"}),
    ):
        await assert_scope_refusal(kind, change, async_client, notification_provider_factory, monkeypatch)


async def test_http_disjoint_settings_writers_commit_only_a_validated_pair(async_client, db_session, monkeypatch):
    from backend.app.api.routes import settings

    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    db_session.add(Settings(key="ha_url", value="http://ha-old.invalid/base"))
    await db_session.commit()
    first_has_anchor = asyncio.Event()
    release = asyncio.Event()
    validated = []
    original = settings.resolve_settings_credentials

    async def resolve(db, changes, actions=None):
        result = await original(db, changes, actions)
        validated.append((result["ha_url"], result["ha_token"]))
        if "ha_url" in changes:
            first_has_anchor.set()
            await release.wait()
        return result

    monkeypatch.setattr(settings, "resolve_settings_credentials", resolve)
    first = asyncio.create_task(async_client.put("/api/v1/settings/", json={"ha_url": "http://ha-new.invalid/base"}))
    second = None
    try:
        await asyncio.wait_for(first_has_anchor.wait(), 10)
        second = asyncio.create_task(async_client.put("/api/v1/settings/", json={"ha_token": "fresh-credential"}))
        await asyncio.sleep(0.1)
        assert not second.done(), "disjoint credential writer bypassed the configuration anchor"
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 20)
        assert [response.status_code for response in results] == [200, 200]
        rows = dict((await db_session.execute(select(Settings.key, Settings.value))).all())
        pair = (rows["ha_url"], rows["ha_token"])
        assert pair == ("http://ha-new.invalid/base", "fresh-credential")
        assert pair in validated, "committed pair was never authorized by either writer"
    finally:
        release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
