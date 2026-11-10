"""The final usable login survives two concurrent, independently owned requests."""

import asyncio

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.auth import create_access_token
from backend.app.core.database import Base, configure_sqlite_connection, import_all_models
from backend.app.models.oidc_provider import OIDCProvider, UserOIDCLink
from backend.app.models.settings import Settings
from backend.app.models.user import User

pytestmark = pytest.mark.integration


@pytest.fixture
async def test_engine(tmp_path, monkeypatch):
    # Independent connections are essential: an in-memory StaticPool cannot
    # prove that another request waits for the account writer to commit.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'g01-sso.sqlite'}")
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    import_all_models()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr("backend.app.core.database.async_session", async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield engine
    finally:
        await engine.dispose()


async def linked_account(db, *, count=1, auth_source="oidc", password=None):
    user = User(username="g01-sso-only", role="user", is_active=True, auth_source=auth_source, password_hash=password)
    db.add(user)
    await db.flush()
    providers = []
    for index in range(count):
        provider = OIDCProvider(name=f"G01 {index}", issuer_url="https://id.example", client_id="client")
        provider.client_secret = "test"
        db.add(provider)
        await db.flush()
        db.add(UserOIDCLink(user_id=user.id, provider_id=provider.id, provider_user_id=f"subject-{index}"))
        providers.append(provider)
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token({'sub': user.username})}"}
    return user, providers, headers


async def test_concurrent_distinct_unlinks_keep_one_login(async_client, db_session, monkeypatch):
    from backend.app.api.routes import mfa

    user, providers, headers = await linked_account(db_session, count=2)
    real_lock = mfa.take_write_lock
    ready = asyncio.Event()
    arrivals = 0

    async def meet_before_account_lock(*args):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        await real_lock(*args)

    monkeypatch.setattr(mfa, "take_write_lock", meet_before_account_lock)
    responses = await asyncio.gather(
        *[async_client.delete(f"/api/v1/auth/oidc/links/{provider.id}", headers=headers) for provider in providers]
    )
    assert sorted(response.status_code for response in responses) == [200, 400], [r.text for r in responses]
    assert (
        await db_session.scalar(select(func.count()).select_from(UserOIDCLink).where(UserOIDCLink.user_id == user.id))
        == 1
    )


@pytest.mark.parametrize(
    "alternative",
    [
        "none",
        "local",
        "local-disabled",
        "local-invalid-policy",
        "oidc-with-hash",
        "ldap",
        "ldap-disabled",
        "disabled-provider",
    ],
)
async def test_unlink_requires_a_usable_alternative(async_client, db_session, monkeypatch, alternative):
    monkeypatch.delenv("BAMDUDE_LOCAL_LOGIN", raising=False)
    user, providers, headers = await linked_account(
        db_session,
        count=2 if alternative == "disabled-provider" else 1,
        auth_source="ldap"
        if alternative.startswith("ldap")
        else "local"
        if alternative.startswith("local")
        else "oidc",
        password="present-hash" if alternative.startswith("local") or alternative == "oidc-with-hash" else None,
    )
    if alternative in {"local-disabled", "local-invalid-policy"}:
        db_session.add(
            Settings(key="local_login_enabled", value="false" if alternative == "local-disabled" else "None")
        )
    if alternative.startswith("ldap"):
        db_session.add_all(
            [
                Settings(key="ldap_enabled", value="true" if alternative == "ldap" else "false"),
                Settings(key="ldap_server_url", value="ldaps://id.example"),
            ]
        )
    if alternative == "disabled-provider":
        providers[1].is_enabled = False
    await db_session.commit()
    response = await async_client.delete(f"/api/v1/auth/oidc/links/{providers[0].id}", headers=headers)
    allowed = alternative in {"local", "ldap"}
    assert response.status_code == (200 if allowed else 400), response.text
    retained = await db_session.scalar(
        select(UserOIDCLink.id).where(UserOIDCLink.user_id == user.id, UserOIDCLink.provider_id == providers[0].id)
    )
    assert (retained is None) == allowed
