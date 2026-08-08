"""Applying (and releasing) the env-declared OIDC provider (upstream #2593).

The row is rewritten on every boot, which is what makes the environment the
authority — and what makes the edges sharp. The cases below are the ones
upstream needed a long tail of follow-up commits to get right, and each is here
because getting it wrong is silent:

* unsetting the variables must **disable**, never delete — the link table
  cascades, so a delete unlinks every bound account permanently;
* the row is matched **by name**, because matching on the flag hit the unique
  constraint during startup and the app would not boot;
* renaming leaves an orphan that must be released, or the next boot finds two
  flagged rows;
* nothing here may raise, because it runs inside the lifespan.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.core.oidc_env import apply_env_oidc_provider
from backend.app.models.group import Group
from backend.app.models.oidc_provider import OIDCProvider

_REQUIRED = {
    "BAMDUDE_OIDC_NAME": "Authentik",
    "BAMDUDE_OIDC_ISSUER_URL": "https://id.example.com",
    "BAMDUDE_OIDC_CLIENT_ID": "bamdude",
    "BAMDUDE_OIDC_CLIENT_SECRET": "s3cr3t",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("BAMDUDE_OIDC_"):
            monkeypatch.delenv(key, raising=False)


def _set(monkeypatch, **overrides):
    for k, v in {**_REQUIRED, **overrides}.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


async def _providers(db) -> list[OIDCProvider]:
    return list((await db.execute(select(OIDCProvider))).scalars().all())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_creates_the_provider_from_the_environment(db_session, monkeypatch):
    _set(monkeypatch)
    await apply_env_oidc_provider(db_session)

    rows = await _providers(db_session)
    assert len(rows) == 1
    assert rows[0].name == "Authentik"
    assert rows[0].issuer_url == "https://id.example.com"
    assert rows[0].is_env_managed is True
    # Through the property, so it is encrypted at rest like any other secret.
    assert rows[0].client_secret == "s3cr3t"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reapplying_updates_in_place_rather_than_duplicating(db_session, monkeypatch):
    _set(monkeypatch)
    await apply_env_oidc_provider(db_session)
    _set(monkeypatch, BAMDUDE_OIDC_ISSUER_URL="https://id2.example.com")
    await apply_env_oidc_provider(db_session)

    rows = await _providers(db_session)
    assert len(rows) == 1
    assert rows[0].issuer_url == "https://id2.example.com"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unsetting_disables_and_releases_but_never_deletes(db_session, monkeypatch):
    """The row survives so the links do.

    ``user_oidc_links.provider_id`` is ``ON DELETE CASCADE``: deleting the
    provider would unlink every bound account, and they would not come back when
    the variables did.
    """
    _set(monkeypatch)
    await apply_env_oidc_provider(db_session)

    for key in _REQUIRED:
        monkeypatch.delenv(key, raising=False)
    await apply_env_oidc_provider(db_session)

    rows = await _providers(db_session)
    assert len(rows) == 1  # still there
    assert rows[0].is_enabled is False
    # Flag cleared too: a provider the API still refuses to edit or delete, with
    # no config behind it, is a dead end reachable only through the database.
    assert rows[0].is_env_managed is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_released_row_loses_its_autologin_claim(db_session, monkeypatch):
    """Otherwise re-enabling it in the UI silently makes it the autologin target
    again — the update route only re-runs the exclusivity sweep when a request
    sets is_autologin=True."""
    _set(monkeypatch, BAMDUDE_OIDC_AUTOLOGIN="true")
    await apply_env_oidc_provider(db_session)
    assert (await _providers(db_session))[0].is_autologin is True

    for key in _REQUIRED:
        monkeypatch.delenv(key, raising=False)
    await apply_env_oidc_provider(db_session)
    assert (await _providers(db_session))[0].is_autologin is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_it_adopts_a_ui_created_provider_of_the_same_name(db_session, monkeypatch):
    """Matched by NAME, which is unique on the table.

    Matching on the flag instead meant an operator who named the env provider
    after an existing one hit that unique constraint inside the lifespan — the
    app would not boot.
    """
    hand_made = OIDCProvider(name="Authentik", issuer_url="https://old.example.com", client_id="old")
    hand_made.client_secret = "old"
    db_session.add(hand_made)
    await db_session.commit()

    _set(monkeypatch)
    await apply_env_oidc_provider(db_session)

    rows = await _providers(db_session)
    assert len(rows) == 1
    assert rows[0].id == hand_made.id
    assert rows[0].issuer_url == "https://id.example.com"
    assert rows[0].is_env_managed is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_renaming_releases_the_row_it_used_to_manage(db_session, monkeypatch):
    """Left flagged, the old row would keep a stale issuer and secret on the login
    page while the API refused every edit — and the next boot would find two
    flagged rows."""
    _set(monkeypatch)
    await apply_env_oidc_provider(db_session)
    _set(monkeypatch, BAMDUDE_OIDC_NAME="Keycloak")
    await apply_env_oidc_provider(db_session)

    rows = {r.name: r for r in await _providers(db_session)}
    assert set(rows) == {"Authentik", "Keycloak"}
    assert rows["Keycloak"].is_env_managed is True
    assert rows["Authentik"].is_env_managed is False
    assert rows["Authentik"].is_enabled is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_bad_boolean_leaves_the_running_provider_alone(db_session, monkeypatch):
    _set(monkeypatch)
    await apply_env_oidc_provider(db_session)

    _set(monkeypatch, BAMDUDE_OIDC_ISSUER_URL="https://new.example.com", BAMDUDE_OIDC_ENABLED="ture")
    await apply_env_oidc_provider(db_session)

    rows = await _providers(db_session)
    assert len(rows) == 1
    # The whole config is skipped, so the new issuer is NOT applied either.
    assert rows[0].issuer_url == "https://id.example.com"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_invalid_issuer_creates_nothing(db_session, monkeypatch):
    # Validated by the same schema the API uses, so env config cannot reach a
    # state the UI would have refused.
    _set(monkeypatch, BAMDUDE_OIDC_ISSUER_URL="http://169.254.169.254/")
    await apply_env_oidc_provider(db_session)
    assert await _providers(db_session) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_default_group_is_resolved_by_name(db_session, monkeypatch):
    group = Group(name="Operators", description="")
    db_session.add(group)
    await db_session.commit()

    _set(monkeypatch, BAMDUDE_OIDC_DEFAULT_GROUP="Operators")
    await apply_env_oidc_provider(db_session)
    assert (await _providers(db_session))[0].default_group_id == group.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_group_name_matching_nothing_refuses_the_whole_config(db_session, monkeypatch):
    """Refused rather than defaulted.

    Falling back would put every auto-created user in Viewers for as long as the
    typo lived — and the API answers 422 for a default_group_id that does not
    exist, so env config gets the same answer.
    """
    _set(monkeypatch, BAMDUDE_OIDC_DEFAULT_GROUP="Nope")
    await apply_env_oidc_provider(db_session)
    assert await _providers(db_session) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_secret_never_reaches_the_log(db_session, monkeypatch, caplog):
    """A ValidationError's str() embeds input_value=… — which is the secret.

    The applier logs ``errors(include_input=False)`` for exactly this reason, and
    falls back to the exception class name for anything unexpected.
    """
    _set(monkeypatch, BAMDUDE_OIDC_CLIENT_SECRET="hunter2" * 200)  # over max_length
    with caplog.at_level("ERROR"):
        await apply_env_oidc_provider(db_session)
    assert await _providers(db_session) == []
    assert "hunter2" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_it_never_raises_even_on_a_broken_session(monkeypatch):
    """It runs inside the lifespan; a raise here is a server that will not start."""

    class _Wedged:
        async def execute(self, *a, **k):
            raise RuntimeError("connection is gone")

        async def rollback(self):
            raise RuntimeError("rollback failed too")

    _set(monkeypatch)
    await apply_env_oidc_provider(_Wedged())  # must not raise
