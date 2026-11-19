"""Reserved repair records survive public settings surfaces without disclosure."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.schema_repair_state import AUDIT, DEBT, RESIDUALS


@pytest.fixture
async def session(tmp_path):
    from backend.app.models.energy_price_history import EnergyPriceHistory
    from backend.app.models.settings import Settings

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Settings.__table__.create)
        # A reset puts the electricity price back to its default, which is a price change on record.
        await conn.run_sync(EnergyPriceHistory.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as result:
        yield result
    await engine.dispose()


async def test_public_reset_preserves_repair_ownership_and_audit(session, monkeypatch):
    from backend.app.api.routes import settings as route
    from backend.app.core.credential_config import CONFIG_GUARD_KEY
    from backend.app.models.settings import Settings

    values = {
        DEBT: "debt-sentinel",
        RESIDUALS: "residual-sentinel",
        AUDIT + "source": "audit-sentinel",
        CONFIG_GUARD_KEY: "anchor",
        "currency": "EUR",
    }
    session.add_all(Settings(key=key, value=value) for key, value in values.items())
    await session.commit()
    monkeypatch.setattr(route, "lock_credential_config", AsyncMock())
    monkeypatch.setattr(route, "_lock_mcp_http_policy", AsyncMock())
    monkeypatch.setattr(route, "_apply_mqtt_runtime", AsyncMock())
    monkeypatch.setattr(route, "get_settings", AsyncMock(return_value=None))
    await route.reset_settings(db=session, _=None)
    remaining = dict((await session.execute(select(Settings.key, Settings.value))).all())
    assert remaining == {k: v for k, v in values.items() if k != "currency"}


async def test_git_collector_excludes_whole_private_namespace(session):
    from backend.app.models.settings import Settings
    from backend.app.services.git_backup import git_backup_service

    values = {DEBT: "private-debt", RESIDUALS: "private-residual", AUDIT + "source": "private-audit", "currency": "EUR"}
    session.add_all(Settings(key=k, value=v) for k, v in values.items())
    await session.commit()
    files = {}
    await git_backup_service._collect_settings(session, files)
    assert files["settings/app_settings.json"]["settings"] == {"currency": "EUR"}


async def test_git_preview_and_apply_cannot_plan_private_changes(session):
    from backend.app.models.settings import Settings
    from backend.app.services.git_restore import git_restore_service

    session.add(Settings(key=DEBT, value="owned"))
    await session.commit()
    plan = await git_restore_service._plan_settings(session, {DEBT: "forged", AUDIT + "arbitrary": "approval"})
    assert plan.accepted == {}
    assert await session.scalar(select(Settings.value).where(Settings.key == DEBT)) == "owned"


async def test_settings_read_model_does_not_expose_private_rows(session):
    from backend.app.api.routes import settings as route
    from backend.app.models.settings import Settings

    session.add(Settings(key=DEBT, value="never-on-the-wire"))
    await session.commit()
    result = await route.get_settings(db=session, _=SimpleNamespace())
    assert "never-on-the-wire" not in result.model_dump_json()
    assert DEBT not in result.model_dump()
