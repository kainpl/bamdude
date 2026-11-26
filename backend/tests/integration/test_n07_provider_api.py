"""N07 spec §7 T4: the provider API and env carry the group mapping, and the revision
changes with every effective policy change and with nothing else."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from backend.app.core import database
from backend.app.core.oidc_env import apply_env_oidc_provider
from backend.app.models.group import Group
from backend.app.models.oidc_provider import OIDCProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE = {"name": "N07 API", "issuer_url": "https://id.example.com", "client_id": "farm", "client_secret": "s"}


async def _group(db, name):
    group = await db.scalar(select(Group).where(Group.name == name))
    if group is None:
        group = Group(name=name, permissions=[])
        db.add(group)
        await db.commit()
    return group


async def _fresh(provider_id):
    async with database.async_session() as session:
        return await session.get(OIDCProvider, provider_id)


async def _raw_mapping(provider_id):
    async with database.async_session() as session:
        return (
            await session.execute(text("SELECT group_mapping FROM oidc_providers WHERE id = :i"), {"i": provider_id})
        ).scalar()


async def test_create_with_a_mapping(async_client, db_session):
    team = await _group(db_session, "Team-API")
    created = await async_client.post(
        "/api/v1/auth/oidc/providers", json={**BASE, "group_claim": "roles", "group_mapping": {" ops ": team.id}}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["group_claim"] == "roles" and body["group_mapping"] == {"ops": team.id}
    assert body["group_mapping_invalid"] is False


async def test_create_without_a_mapping_has_none(async_client):
    created = await async_client.post("/api/v1/auth/oidc/providers", json=BASE)
    assert created.status_code == 201, created.text
    assert created.json()["group_mapping"] == {} and created.json()["group_claim"] == "groups"


@pytest.mark.parametrize(
    "mapping",
    [
        {"ops": True},
        {"ops": 0},
        {"ops": 2**31},
        {"ops": "3"},
        {"ops": 1, " ops": 1},
        [],
        {"": 1},
    ],
)
async def test_create_refuses_a_malformed_mapping_before_any_query(async_client, mapping):
    created = await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "group_mapping": mapping})
    assert created.status_code == 422, created.text


async def test_create_refuses_groups_that_do_not_exist(async_client):
    created = await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "group_mapping": {"ops": 2**31 - 1}})
    assert created.status_code == 422, created.text
    assert "2147483647" in created.text


async def test_put_null_keeps_the_mapping_and_empty_object_clears_it(async_client, db_session):
    team = await _group(db_session, "Team-API")
    pid = (
        await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "group_mapping": {"ops": team.id}})
    ).json()["id"]
    kept = await async_client.put(
        f"/api/v1/auth/oidc/providers/{pid}", json={"group_mapping": None, "name": "N07 renamed"}
    )
    assert kept.status_code == 200 and kept.json()["group_mapping"] == {"ops": team.id}
    cleared = await async_client.put(f"/api/v1/auth/oidc/providers/{pid}", json={"group_mapping": {}})
    assert cleared.status_code == 200 and cleared.json()["group_mapping"] == {}


async def test_the_revision_follows_policy_fields_only(async_client, db_session):
    team = await _group(db_session, "Team-API")
    first = (await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "is_autologin": True})).json()["id"]
    second = (await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "name": "N07 second"})).json()["id"]
    revision = (await _fresh(first)).policy_revision

    for change in ({"name": "N07 meta"}, {"is_autologin": True}):
        assert (await async_client.put(f"/api/v1/auth/oidc/providers/{first}", json=change)).status_code == 200
        assert (await _fresh(first)).policy_revision == revision, change

    # Another provider taking the autologin sweeps this one's flag set-based: no policy change.
    other_revision = (await _fresh(second)).policy_revision
    assert (
        await async_client.put(f"/api/v1/auth/oidc/providers/{second}", json={"is_autologin": True})
    ).status_code == 200
    assert (await _fresh(first)).policy_revision == revision
    assert (await _fresh(second)).policy_revision == other_revision

    for change in (
        {"group_mapping": {"ops": team.id}},
        {"group_claim": "roles"},
        {"scopes": "openid email"},
        {"client_secret": "rotated"},
        {"is_enabled": False},
    ):
        assert (await async_client.put(f"/api/v1/auth/oidc/providers/{first}", json=change)).status_code == 200
        changed = (await _fresh(first)).policy_revision
        assert changed != revision, change
        revision = changed

    # The same value again is no change.
    assert (
        await async_client.put(f"/api/v1/auth/oidc/providers/{first}", json={"group_claim": "roles"})
    ).status_code == 200
    assert (await _fresh(first)).policy_revision == revision


async def test_a_damaged_mapping_is_shown_and_replaced_explicitly(async_client, db_session):
    healthy = (await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "name": "N07 healthy"})).json()[
        "id"
    ]
    damaged = (await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "name": "N07 damaged"})).json()[
        "id"
    ]
    async with database.async_session() as session:
        await session.execute(text("UPDATE oidc_providers SET group_mapping = 'null' WHERE id = :i"), {"i": damaged})
        await session.commit()

    listed = {p["id"]: p for p in (await async_client.get("/api/v1/auth/oidc/providers/all")).json()}
    assert listed[healthy]["group_mapping_invalid"] is False
    assert listed[damaged]["group_mapping"] is None and listed[damaged]["group_mapping_invalid"] is True

    for body in ({"name": "N07 damaged renamed"}, {"group_mapping": None}):
        kept = await async_client.put(f"/api/v1/auth/oidc/providers/{damaged}", json=body)
        assert kept.status_code == 200, kept.text
        assert kept.json()["group_mapping"] is None and kept.json()["group_mapping_invalid"] is True

    revision = (await _fresh(damaged)).policy_revision
    replaced = await async_client.put(f"/api/v1/auth/oidc/providers/{damaged}", json={"group_mapping": {}})
    assert replaced.json()["group_mapping"] == {} and replaced.json()["group_mapping_invalid"] is False
    assert (await _fresh(damaged)).policy_revision != revision


async def test_true_replaced_by_one_reaches_the_row(async_client, db_session):
    group_one = await db_session.get(Group, 1)
    assert group_one is not None  # the seeded Administrators
    pid = (await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "name": "N07 strict"})).json()["id"]
    async with database.async_session() as session:
        await session.execute(
            text("""UPDATE oidc_providers SET group_mapping = '{"team": true}' WHERE id = :i"""), {"i": pid}
        )
        await session.commit()
    revision = (await _fresh(pid)).policy_revision
    replaced = await async_client.put(f"/api/v1/auth/oidc/providers/{pid}", json={"group_mapping": {"team": 1}})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["group_mapping"] == {"team": 1} and replaced.json()["group_mapping_invalid"] is False
    assert (await _raw_mapping(pid)).replace(" ", "") == '{"team":1}'
    assert (await _fresh(pid)).policy_revision != revision


async def test_the_public_list_carries_no_mapping(async_client, db_session):
    team = await _group(db_session, "Team-API")
    await async_client.post("/api/v1/auth/oidc/providers", json={**BASE, "group_mapping": {"ops": team.id}})
    public = (await async_client.get("/api/v1/auth/oidc/providers")).json()
    assert public and all("group_mapping" not in p and "group_claim" not in p for p in public)


# ── env (spec §F) ────────────────────────────────────────────────────────────

_ENV = {
    "BAMDUDE_OIDC_NAME": "N07 env",
    "BAMDUDE_OIDC_ISSUER_URL": "https://id.example.com",
    "BAMDUDE_OIDC_CLIENT_ID": "bamdude",
    "BAMDUDE_OIDC_CLIENT_SECRET": "s3cr3t",
}


@pytest.fixture
def env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("BAMDUDE_OIDC_"):
            monkeypatch.delenv(key, raising=False)

    def apply(**overrides):
        for key, value in {**_ENV, **overrides}.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)

    return apply


async def _env_row(db):
    return await db.scalar(
        select(OIDCProvider).where(OIDCProvider.name == "N07 env").execution_options(populate_existing=True)
    )


async def test_env_names_become_ids_and_an_unchanged_env_keeps_the_revision(db_session, env):
    team = await _group(db_session, "Team-Env")
    env(BAMDUDE_OIDC_GROUP_CLAIM="roles", BAMDUDE_OIDC_GROUP_MAPPING='{"ops": "Team-Env"}')
    await apply_env_oidc_provider(db_session)
    row = await _env_row(db_session)
    assert row.group_claim == "roles" and row.group_mapping == {"ops": team.id}
    revision = row.policy_revision
    await apply_env_oidc_provider(db_session)  # a restart with the same environment
    assert (await _env_row(db_session)).policy_revision == revision


@pytest.mark.parametrize("mapping", ['{"ops": "No such group"}', "not json", '["Team-Env"]', '{"ops": 3}'])
async def test_a_bad_env_mapping_leaves_the_running_provider(db_session, env, mapping):
    await _group(db_session, "Team-Env")
    env(BAMDUDE_OIDC_GROUP_MAPPING='{"ops": "Team-Env"}')
    await apply_env_oidc_provider(db_session)
    before = await _env_row(db_session)
    revision, kept = before.policy_revision, dict(before.group_mapping)
    env(BAMDUDE_OIDC_GROUP_MAPPING=mapping, BAMDUDE_OIDC_CLIENT_ID="changed")
    await apply_env_oidc_provider(db_session)
    after = await _env_row(db_session)
    assert after.group_mapping == kept and after.client_id == "bamdude" and after.policy_revision == revision


async def test_env_without_the_new_variables_resets_to_the_defaults(db_session, env):
    await _group(db_session, "Team-Env")
    env(BAMDUDE_OIDC_GROUP_CLAIM="roles", BAMDUDE_OIDC_GROUP_MAPPING='{"ops": "Team-Env"}')
    await apply_env_oidc_provider(db_session)
    env(BAMDUDE_OIDC_GROUP_CLAIM=None, BAMDUDE_OIDC_GROUP_MAPPING=None)
    await apply_env_oidc_provider(db_session)
    row = await _env_row(db_session)
    assert row.group_claim == "groups" and row.group_mapping == {}


async def test_releasing_an_enabled_env_row_changes_its_revision_and_a_disabled_one_keeps_it(db_session, env):
    env()
    await apply_env_oidc_provider(db_session)
    enabled = await _env_row(db_session)
    enabled_id, enabled_revision = enabled.id, enabled.policy_revision
    env(BAMDUDE_OIDC_NAME="N07 env renamed")  # the rename releases the previous row, set-based
    await apply_env_oidc_provider(db_session)
    released = await db_session.get(OIDCProvider, enabled_id, populate_existing=True)
    assert not released.is_enabled and not released.is_env_managed
    assert released.policy_revision != enabled_revision

    # A row that was already disabled keeps its revision when it is released.
    renamed = await db_session.scalar(select(OIDCProvider).where(OIDCProvider.name == "N07 env renamed"))
    env(BAMDUDE_OIDC_NAME="N07 env renamed", BAMDUDE_OIDC_ENABLED="false")
    await apply_env_oidc_provider(db_session)
    disabled_revision = (await db_session.get(OIDCProvider, renamed.id, populate_existing=True)).policy_revision
    env(BAMDUDE_OIDC_NAME="N07 env third")
    await apply_env_oidc_provider(db_session)
    assert (await db_session.get(OIDCProvider, renamed.id, populate_existing=True)).policy_revision == disabled_revision
