"""N07 — OIDC groups follow the IdP at every issued session (spec §7 T2, T6).

Only the IdP's HTTP is a stub; the ID-token signature, nonce, one-time rows,
factors and sessions run through the real routes on the test SQLite. Every
outcome is read back through a NEW database session after the request.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pyotp
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from backend.app.api.routes import mfa
from backend.app.core import database
from backend.app.core.auth import REFRESH_TOKEN_COOKIE_NAME
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType
from backend.app.models.group import Group
from backend.app.models.oidc_provider import OIDCProvider, UserOIDCLink
from backend.app.models.user import User
from backend.app.models.user_totp import UserTOTP
from backend.app.services import oidc_group_sync

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ISSUER = "https://idp.invalid"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class IdPStub:
    """The IdP's discovery, token and JWKS endpoints; ``claims`` shape the next ID token."""

    def __init__(self, key):
        self.key = key
        self.nonce = None
        self.calls: list[str] = []
        self.claims: dict = {}
        self.before_token = None  # an async hook run inside the token call (mid-network changes)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "n07-key"
        self.jwk = jwk

    def client(self, **_kwargs):
        stub = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url):
                stub.calls.append(url)
                if url.endswith("/jwks"):
                    data = {"keys": [stub.jwk]}
                else:
                    data = {
                        "issuer": ISSUER,
                        "authorization_endpoint": f"{ISSUER}/authorize",
                        "token_endpoint": f"{ISSUER}/token",
                        "jwks_uri": f"{ISSUER}/jwks",
                    }
                return httpx.Response(200, json=data, request=httpx.Request("GET", url))

            async def post(self, url, **kwargs):
                stub.calls.append(url)
                if stub.before_token is not None:
                    await stub.before_token()
                payload = {
                    "iss": ISSUER,
                    "aud": "farm",
                    "nonce": stub.nonce,
                    "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
                }
                payload.update(stub.claims)
                payload = {k: v for k, v in payload.items() if v is not _DROP}
                token = jwt.encode(payload, stub.key, algorithm="RS256", headers={"kid": "n07-key"})
                return httpx.Response(200, json={"id_token": token}, request=httpx.Request("POST", url))

        return Client()


_DROP = object()


@pytest.fixture
async def n07(async_client, db_session, monkeypatch, signing_key):
    stub = IdPStub(signing_key)
    monkeypatch.setattr(mfa.httpx, "AsyncClient", stub.client)

    class Harness:
        idp = stub
        client = async_client
        db = db_session

        async def group(self, name):
            existing = await db_session.scalar(select(Group).where(Group.name == name))
            if existing is not None:
                return existing
            group = Group(name=name, permissions=[])
            db_session.add(group)
            await db_session.flush()
            return group

        async def provider(self, mapping=None, **fields):
            values = {
                "name": f"N07 IdP {len(stub.calls)}-{id(fields)}",
                "issuer_url": ISSUER,
                "client_id": "farm",
                "client_secret": "secret",
                "group_claim": "groups",
                "group_mapping": mapping or {},
            }
            values.update(fields)
            provider = OIDCProvider(**values)
            db_session.add(provider)
            await db_session.flush()
            return provider

        async def user(self, username, provider, *, sub, groups=(), role="user", email=None):
            user = User(username=username, auth_source="oidc", role=role, is_active=True, email=email)
            user.groups = list(groups)
            db_session.add(user)
            await db_session.flush()
            db_session.add(UserOIDCLink(user_id=user.id, provider_id=provider.id, provider_user_id=sub))
            await db_session.flush()
            return user

        async def callback(self, provider, *, sub, groups=_DROP, **claims):
            """authorize → callback; returns the callback's Location."""
            await db_session.commit()
            authorize = await async_client.get(f"/api/v1/auth/oidc/authorize/{provider.id}")
            assert authorize.status_code == 200, authorize.text
            query = parse_qs(urlsplit(authorize.json()["auth_url"]).query)
            stub.nonce = query["nonce"][0]
            stub.claims = {"sub": sub, "groups": groups, **claims}
            callback = await async_client.get(
                "/api/v1/auth/oidc/callback",
                params={"state": query["state"][0], "code": "valid-code"},
                follow_redirects=False,
            )
            assert callback.status_code == 302, callback.text
            return callback.headers["location"]

        async def login(self, provider, *, sub, groups=_DROP, **claims):
            location = await self.callback(provider, sub=sub, groups=groups, **claims)
            assert "#oidc_token=" in location, location
            return parse_qs(urlsplit(location).fragment)["oidc_token"][0]

        async def exchange(self, token):
            return await async_client.post("/api/v1/auth/oidc/exchange", json={"oidc_token": token})

    return Harness()


async def fresh_user(user_id):
    async with database.async_session() as session:
        return (
            await session.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))
        ).scalar_one()


async def fresh_groups(user_id) -> set[str]:
    return {g.name for g in (await fresh_user(user_id)).groups}


async def fresh_scalar(sql, **params):
    async with database.async_session() as session:
        return (await session.execute(text(sql), params)).scalar()


def error_code(location: str) -> str:
    parts = urlsplit(location)
    assert parts.path == "/login", location
    return parse_qs(parts.query)["oidc_error"][0]


def refusal(response) -> str:
    assert response.status_code == 409, response.text
    return response.json()["detail"]["error"]


# ── the managed slice, at issuance only ──────────────────────────────────────


async def test_exchange_applies_the_snapshot_and_keeps_manual_groups(n07):
    a, b, manual = await n07.group("Team-A"), await n07.group("Team-B"), await n07.group("Manual")
    provider = await n07.provider({"idp-a": a.id, "idp-b": b.id})
    user = await n07.user("n07-one", provider, sub="s-one", groups=[a, manual])

    token = await n07.login(provider, sub="s-one", groups=["idp-b"])
    assert await fresh_groups(user.id) == {"Team-A", "Manual"}  # the callback applied nothing

    exchanged = await n07.exchange(token)
    assert exchanged.status_code == 200, exchanged.text
    assert {g["name"] for g in exchanged.json()["user"]["groups"]} == {"Team-B", "Manual"}
    assert await fresh_groups(user.id) == {"Team-B", "Manual"}


async def test_empty_mapping_issues_a_session_without_touching_groups(n07):
    a = await n07.group("Team-A")
    provider = await n07.provider({})
    user = await n07.user("n07-empty", provider, sub="s-empty", groups=[a])
    exchanged = await n07.exchange(await n07.login(provider, sub="s-empty", groups=["anything"]))
    assert exchanged.status_code == 200, exchanged.text
    assert await fresh_groups(user.id) == {"Team-A"}


async def test_missing_claim_removes_the_managed_groups(n07):
    a, manual = await n07.group("Team-A"), await n07.group("Manual")
    provider = await n07.provider({"idp-a": a.id})
    user = await n07.user("n07-noclaim", provider, sub="s-noclaim", groups=[a, manual])
    exchanged = await n07.exchange(await n07.login(provider, sub="s-noclaim"))
    assert exchanged.status_code == 200, exchanged.text
    assert await fresh_groups(user.id) == {"Manual"}


async def test_narrowed_mapping_leaves_the_former_managed_group(n07):
    a, b = await n07.group("Team-A"), await n07.group("Team-B")
    provider = await n07.provider({"idp-b": b.id})  # Team-A is no longer managed
    user = await n07.user("n07-narrow", provider, sub="s-narrow", groups=[a, b])
    exchanged = await n07.exchange(await n07.login(provider, sub="s-narrow", groups=[]))
    assert exchanged.status_code == 200, exchanged.text
    assert await fresh_groups(user.id) == {"Team-A"}


async def test_totp_applies_only_at_verify(n07):
    a, b = await n07.group("Team-A"), await n07.group("Team-B")
    provider = await n07.provider({"idp-a": a.id, "idp-b": b.id})
    user = await n07.user("n07-totp", provider, sub="s-totp", groups=[a])
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = pyotp.random_base32()
    n07.db.add(factor)

    exchanged = await n07.exchange(await n07.login(provider, sub="s-totp", groups=["idp-b"]))
    assert exchanged.status_code == 200 and exchanged.json()["requires_2fa"], exchanged.text
    assert await fresh_groups(user.id) == {"Team-A"}
    pre_auth = exchanged.json()["pre_auth_token"]

    wrong = await n07.client.post(
        "/api/v1/auth/2fa/verify", json={"pre_auth_token": pre_auth, "code": "000000", "method": "totp"}
    )
    assert wrong.status_code == 401
    assert await fresh_groups(user.id) == {"Team-A"}

    verified = await n07.client.post(
        "/api/v1/auth/2fa/verify",
        json={"pre_auth_token": pre_auth, "code": pyotp.TOTP(factor.secret).now(), "method": "totp"},
    )
    assert verified.status_code == 200, verified.text
    assert await fresh_groups(user.id) == {"Team-B"}


async def test_email_resend_carries_the_same_evidence_and_verify_applies(n07, monkeypatch):
    a, b = await n07.group("Team-A"), await n07.group("Team-B")
    provider = await n07.provider({"idp-a": a.id, "idp-b": b.id})
    user = await n07.user("n07-mail", provider, sub="s-mail", groups=[a], email="mail@example.invalid")
    await mfa._set_email_2fa_enabled(n07.db, user.id, True)
    sent_mail = {}
    monkeypatch.setattr(mfa, "get_smtp_settings", AsyncMock(return_value={"host": "offline"}))
    monkeypatch.setattr(mfa, "send_email", lambda **kwargs: sent_mail.update(kwargs))

    exchanged = await n07.exchange(await n07.login(provider, sub="s-mail", groups=["idp-b"]))
    pre_auth = exchanged.json()["pre_auth_token"]
    first = await fresh_scalar("SELECT oidc_login_evidence FROM auth_ephemeral_tokens WHERE token = :t", t=pre_auth)
    for _ in range(2):
        sent = await n07.client.post("/api/v1/auth/2fa/email/send", json={"pre_auth_token": pre_auth})
        assert sent.status_code == 200, sent.text
        pre_auth = sent.json()["pre_auth_token"]
        carried = await fresh_scalar(
            "SELECT oidc_login_evidence FROM auth_ephemeral_tokens WHERE token = :t", t=pre_auth
        )
        assert json.loads(carried) == json.loads(first)  # same evidence, same deadline
    assert await fresh_groups(user.id) == {"Team-A"}
    code = sent_mail["body_text"].split("code is: ")[1].split("\n")[0]
    verified = await n07.client.post(
        "/api/v1/auth/2fa/verify", json={"pre_auth_token": pre_auth, "code": code, "method": "email"}
    )
    assert verified.status_code == 200, verified.text
    assert await fresh_groups(user.id) == {"Team-B"}


# ── the provider and the link must still be the ones the attempt started under ──


@pytest.mark.parametrize("change", ["disable", "disable_enable", "mapping", "claim", "secret", "auto_link"])
async def test_a_policy_change_after_the_callback_refuses_the_exchange(n07, change):
    a, b = await n07.group("Team-A"), await n07.group("Team-B")
    provider = await n07.provider({"idp-a": a.id})
    user = await n07.user(f"n07-chg-{change}", provider, sub=f"s-{change}", groups=[a])
    token = await n07.login(provider, sub=f"s-{change}", groups=[])

    async with database.async_session() as session:
        row = await session.get(OIDCProvider, provider.id)
        if change in {"disable", "disable_enable"}:
            row.is_enabled = False
            if change == "disable_enable":
                await session.commit()
                row = await session.get(OIDCProvider, provider.id)
                row.is_enabled = True
        elif change == "mapping":
            from backend.app.models.oidc_provider import assign_group_mapping

            assign_group_mapping(row, {"idp-a": a.id, "idp-b": b.id})
        elif change == "claim":
            row.group_claim = "roles"
        elif change == "secret":
            row.client_secret = "rotated"
        elif change == "auto_link":
            row.auto_link_existing_accounts = not row.auto_link_existing_accounts
        await session.commit()

    assert refusal(await n07.exchange(token)) == oidc_group_sync.POLICY_CHANGED
    assert await fresh_groups(user.id) == {"Team-A"}
    # Rolled back whole: the exchange row is still there, and no session was issued for it.
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token = :t", t=token) == 1


async def test_a_metadata_change_keeps_the_attempt(n07):
    provider = await n07.provider({})
    user = await n07.user("n07-meta", provider, sub="s-meta")
    token = await n07.login(provider, sub="s-meta")
    before = provider.policy_revision
    async with database.async_session() as session:
        row = await session.get(OIDCProvider, provider.id)
        row.name = "renamed"
        row.is_autologin = True
        await session.commit()
        assert row.policy_revision == before
    assert (await n07.exchange(token)).status_code == 200
    assert user.id


async def test_a_recreated_provider_with_the_same_id_is_another_provider(n07):
    provider = await n07.provider({})
    user = await n07.user("n07-recreate", provider, sub="s-recreate")
    token = await n07.login(provider, sub="s-recreate")
    provider_id, name = provider.id, provider.name
    async with database.async_session() as session:
        await session.delete(await session.get(OIDCProvider, provider_id))
        await session.commit()
        twin = OIDCProvider(
            id=provider_id, name=name, issuer_url=ISSUER, client_id="farm", client_secret="secret", group_mapping={}
        )
        session.add(twin)
        await session.flush()
        session.add(UserOIDCLink(user_id=user.id, provider_id=provider_id, provider_user_id="s-recreate"))
        await session.commit()
    assert refusal(await n07.exchange(token)) == oidc_group_sync.POLICY_CHANGED


async def test_a_change_before_the_callback_reaches_no_idp(n07):
    provider = await n07.provider({})
    await n07.user("n07-pre", provider, sub="s-pre")
    await n07.db.commit()
    authorize = await n07.client.get(f"/api/v1/auth/oidc/authorize/{provider.id}")
    state = parse_qs(urlsplit(authorize.json()["auth_url"]).query)["state"][0]
    async with database.async_session() as session:
        (await session.get(OIDCProvider, provider.id)).is_enabled = False
        await session.commit()
    n07.idp.calls.clear()
    callback = await n07.client.get(
        "/api/v1/auth/oidc/callback", params={"state": state, "code": "c"}, follow_redirects=False
    )
    assert error_code(callback.headers["location"]) == oidc_group_sync.PROVIDER_CHANGED
    assert n07.idp.calls == []
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token = :t", t=state) == 0


async def test_a_change_during_the_network_phase_writes_nothing(n07):
    provider = await n07.provider({}, auto_create_users=True)

    async def change():
        async with database.async_session() as session:
            (await session.get(OIDCProvider, provider.id)).client_id = "other"
            await session.commit()

    n07.idp.before_token = change
    location = await n07.callback(provider, sub="s-midway")
    assert error_code(location) == oidc_group_sync.PROVIDER_CHANGED
    assert await fresh_scalar("SELECT count(*) FROM user_oidc_links WHERE provider_user_id = 's-midway'") == 0
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token_type = 'oidc_exchange'") == 0


async def test_a_legacy_state_is_refused(n07):
    provider = await n07.provider({})
    n07.db.add(
        AuthEphemeralToken(
            token="legacy-state",
            token_type=TokenType.OIDC_STATE,
            provider_id=provider.id,
            nonce="n",
            code_verifier="v",
            contract_version=1,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await n07.db.commit()
    callback = await n07.client.get(
        "/api/v1/auth/oidc/callback", params={"state": "legacy-state", "code": "c"}, follow_redirects=False
    )
    assert error_code(callback.headers["location"]) == "invalid_state"


async def test_unlink_revokes_the_unfinished_attempt(n07):
    provider = await n07.provider({})
    user = await n07.user("n07-unlink", provider, sub="s-unlink")
    token = await n07.login(provider, sub="s-unlink")
    async with database.async_session() as session:
        await session.execute(
            text(
                "INSERT INTO auth_ephemeral_tokens (token, token_type, auth_source, account_identity, expires_at, "
                "created_at, contract_version) VALUES ('evidence-less', 'pre_auth', 'oidc', :a, :e, :e, 0)"
            ),
            {"a": user.account_identity, "e": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)},
        )
        await session.commit()
    from backend.tests.auth_helpers import mint_test_access

    access = await mint_test_access(user=user)
    # A second login method, so the link is not the last one.
    async with database.async_session() as session:
        await session.execute(
            text("UPDATE users SET password_hash = 'x', auth_source = 'local' WHERE id = :i"), {"i": user.id}
        )
        await session.commit()
    removed = await n07.client.delete(
        f"/api/v1/auth/oidc/links/{provider.id}", headers={"Authorization": f"Bearer {access}"}
    )
    assert removed.status_code == 200, removed.text
    assert (
        await fresh_scalar(
            "SELECT count(*) FROM auth_ephemeral_tokens WHERE account_identity = :a AND auth_source = 'oidc'",
            a=user.account_identity,
        )
        == 0
    )
    assert (await n07.exchange(token)).status_code == 401


# ── evidence (spec §C.2) ─────────────────────────────────────────────────────


async def test_an_exchange_without_evidence_leads_to_a_new_login(n07):
    from backend.app.core.auth_lifetime import binding, capture_account

    provider = await n07.provider({})
    user = await n07.user("n07-legacy", provider, sub="s-legacy")
    n07.db.add(
        AuthEphemeralToken(
            token="legacy-exchange",
            token_type=TokenType.OIDC_EXCHANGE,
            auth_source="oidc",
            username=user.username,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            **binding(capture_account(user)),
        )
    )
    await n07.db.commit()
    assert refusal(await n07.exchange("legacy-exchange")) == oidc_group_sync.LOGIN_EXPIRED
    assert (
        await fresh_scalar(
            "SELECT count(*) FROM auth_ephemeral_tokens WHERE token = 'legacy-exchange' AND oidc_login_evidence IS NULL"
        )
        == 1
    )


async def test_the_deadline_stops_the_exchange(n07, monkeypatch):
    provider = await n07.provider({})
    await n07.user("n07-deadline", provider, sub="s-deadline")
    token = await n07.login(provider, sub="s-deadline")
    deadline = json.loads(
        await fresh_scalar("SELECT oidc_login_evidence FROM auth_ephemeral_tokens WHERE token = :t", t=token)
    )["deadline"]
    monkeypatch.setattr(oidc_group_sync, "now_utc", lambda: datetime.fromtimestamp(deadline, tz=timezone.utc))
    assert refusal(await n07.exchange(token)) == oidc_group_sync.LOGIN_EXPIRED


@pytest.mark.parametrize("exp", [_DROP, None, True, "9999999999", float("inf")])
async def test_an_unusable_exp_is_refused(n07, exp):
    provider = await n07.provider({})
    await n07.user(f"n07-exp-{exp!r}", provider, sub=f"s-exp-{exp!r}")
    if isinstance(exp, float):
        pytest.skip("PyJWT cannot encode an infinite exp; the validator is covered by the unit test")
    location = await n07.callback(provider, sub=f"s-exp-{exp!r}", exp=exp)
    assert error_code(location) == "token_validation_failed"


async def test_a_fractional_exp_is_valid_until_its_whole_second(n07):
    provider = await n07.provider({})
    await n07.user("n07-frac", provider, sub="s-frac")
    exp = datetime.now(timezone.utc).timestamp() + 120.75
    token = await n07.login(provider, sub="s-frac", exp=exp)
    evidence = json.loads(
        await fresh_scalar("SELECT oidc_login_evidence FROM auth_ephemeral_tokens WHERE token = :t", t=token)
    )
    assert evidence["deadline"] == int(exp)


# ── the claim (spec §D) ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "claims,code",
    [
        ({"hasgroups": True}, oidc_group_sync.CLAIMS_INCOMPLETE),
        ({"groups": ["a"], "_claim_names": {"groups": "src1"}}, oidc_group_sync.CLAIMS_INCOMPLETE),
        ({"groups": None, "hasgroups": True}, oidc_group_sync.CLAIMS_INCOMPLETE),
        ({"groups": ["a", 1]}, oidc_group_sync.CLAIMS_INVALID),
        ({"groups": {"a": 1}}, oidc_group_sync.CLAIMS_INVALID),
        ({"_claim_names": "groups"}, oidc_group_sync.CLAIMS_INVALID),
    ],
)
async def test_a_claim_that_cannot_be_read_refuses_before_any_write(n07, claims, code):
    a = await n07.group("Team-A")
    provider = await n07.provider({"idp-a": a.id}, auto_create_users=True)
    sub = f"s-claims-{code}-{len(claims)}"
    location = await n07.callback(provider, sub=sub, **claims)
    assert error_code(location) == code
    assert await fresh_scalar("SELECT count(*) FROM user_oidc_links WHERE provider_user_id = :s", s=sub) == 0
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token_type = 'oidc_exchange'") == 0


# ── a damaged stored mapping (spec §F, S45) ──────────────────────────────────


async def test_a_damaged_mapping_refuses_the_callback(n07):
    provider = await n07.provider({})
    await n07.user("n07-damaged-cb", provider, sub="s-damaged-cb")
    await n07.db.execute(
        text("UPDATE oidc_providers SET group_mapping = :m WHERE id = :i"), {"m": '["x"]', "i": provider.id}
    )
    location = await n07.callback(provider, sub="s-damaged-cb")
    assert error_code(location) == oidc_group_sync.MAPPING_INVALID


async def test_a_damaged_mapping_refuses_the_exchange_with_the_revision_unchanged(n07):
    a = await n07.group("Team-A")
    provider = await n07.provider({"idp-a": a.id})
    user = await n07.user("n07-damaged-ex", provider, sub="s-damaged-ex", groups=[a])
    token = await n07.login(provider, sub="s-damaged-ex", groups=["idp-a"])
    async with database.async_session() as session:  # raw: the revision stays the attempt's
        await session.execute(
            text("UPDATE oidc_providers SET group_mapping = :m WHERE id = :i"),
            {"m": '{"idp-a": true}', "i": provider.id},
        )
        await session.commit()
    assert refusal(await n07.exchange(token)) == oidc_group_sync.MAPPING_INVALID
    assert await fresh_groups(user.id) == {"Team-A"}


# ── a new account (spec §C.7, owner S12) ─────────────────────────────────────


async def test_a_new_account_gets_its_default_group_and_the_managed_part_at_the_first_session(n07):
    a, default = await n07.group("Team-A"), await n07.group("Default-N07")
    provider = await n07.provider({"idp-a": a.id}, auto_create_users=True, default_group_id=default.id)
    token = await n07.login(provider, sub="s-new", groups=["idp-a"], email="new@example.invalid")
    user_id = await fresh_scalar("SELECT user_id FROM user_oidc_links WHERE provider_user_id = 's-new'")
    assert await fresh_groups(user_id) == set()
    assert await fresh_scalar("SELECT first_login_pending FROM user_oidc_links WHERE user_id = :u", u=user_id) == 1
    exchanged = await n07.exchange(token)
    assert exchanged.status_code == 200, exchanged.text
    assert await fresh_groups(user_id) == {"Team-A", "Default-N07"}
    assert await fresh_scalar("SELECT first_login_pending FROM user_oidc_links WHERE user_id = :u", u=user_id) == 0


async def test_an_abandoned_first_login_leaves_no_rights_and_the_next_one_gives_the_default(n07):
    admins = await n07.group("Administrators")
    provider = await n07.provider({}, auto_create_users=True, default_group_id=admins.id)
    await n07.login(provider, sub="s-abandoned")  # never exchanged
    user_id = await fresh_scalar("SELECT user_id FROM user_oidc_links WHERE provider_user_id = 's-abandoned'")
    user = await fresh_user(user_id)
    assert not user.groups and user.role == "user" and not user.is_admin

    # It does not count as "another active administrator": the real last admin keeps the guard.
    from backend.app.core.admin_membership import require_other_active_admin

    others = await fresh_scalar(
        "SELECT count(*) FROM users u JOIN user_groups ug ON ug.user_id = u.id JOIN groups g ON g.id = ug.group_id "
        "WHERE g.name = 'Administrators' AND u.id = :u",
        u=user_id,
    )
    assert others == 0
    assert require_other_active_admin  # the guard counts group or role; this account has neither

    second = await n07.exchange(await n07.login(provider, sub="s-abandoned"))
    assert second.status_code == 200, second.text
    assert await fresh_groups(user_id) == {"Administrators"}


async def test_a_default_administrators_removed_by_the_managed_part_is_no_demotion(n07):
    admins = await n07.group("Administrators")
    provider = await n07.provider({"idp-admins": admins.id}, auto_create_users=True, default_group_id=admins.id)
    exchanged = await n07.exchange(await n07.login(provider, sub="s-default-admin", groups=[]))
    assert exchanged.status_code == 200, exchanged.text
    user_id = await fresh_scalar("SELECT user_id FROM user_oidc_links WHERE provider_user_id = 's-default-admin'")
    assert await fresh_groups(user_id) == set()


async def test_auto_link_sets_no_pending_flag(n07):
    target = User(username="n07-local", auth_source="local", role="user", is_active=True, email="link@example.invalid")
    n07.db.add(target)
    provider = await n07.provider({}, auto_link_existing_accounts=True, email_claim="preferred_username")
    token = await n07.login(provider, sub="s-autolink", preferred_username="link@example.invalid")
    assert (
        await fresh_scalar("SELECT first_login_pending FROM user_oidc_links WHERE provider_user_id = 's-autolink'") == 0
    )
    assert (await n07.exchange(token)).status_code == 200


# ── administrators (spec §E) ─────────────────────────────────────────────────


async def test_the_last_administrator_is_not_removed_by_the_idp(n07):
    admins = await n07.group("Administrators")
    provider = await n07.provider({"idp-admins": admins.id})
    async with database.async_session() as session:  # the fixture admin stops being active
        await session.execute(text("UPDATE users SET is_active = 0 WHERE username = 'test_admin'"))
        await session.commit()
    user = await n07.user("n07-last-admin", provider, sub="s-last", groups=[admins], role="admin")
    token = await n07.login(provider, sub="s-last", groups=[])
    assert refusal(await n07.exchange(token)) == oidc_group_sync.LAST_ADMIN
    after = await fresh_user(user.id)
    assert after.role == "admin" and {g.name for g in after.groups} == {"Administrators"}


async def test_the_legacy_admin_role_follows_a_managed_administrators(n07):
    admins = await n07.group("Administrators")
    provider = await n07.provider({"idp-admins": admins.id})
    user = await n07.user("n07-legacy-admin", provider, sub="s-legacy-admin", role="admin")
    exchanged = await n07.exchange(await n07.login(provider, sub="s-legacy-admin", groups=[]))
    assert exchanged.status_code == 200, exchanged.text
    assert (await fresh_user(user.id)).role == "user"


# ── refusal addresses (S30, S38) and existing sessions (T6) ──────────────────


async def test_callback_refusals_land_on_the_login_page(n07):
    location = (
        await n07.client.get("/api/v1/auth/oidc/callback", params={"error": "access_denied"}, follow_redirects=False)
    ).headers["location"]
    assert error_code(location) == "oidc_provider_error"


async def test_an_existing_session_reads_the_new_rights_on_its_next_request(n07):
    a = await n07.group("Team-A")
    provider = await n07.provider({"idp-a": a.id})
    user = await n07.user("n07-session", provider, sub="s-session", groups=[a])
    from backend.tests.auth_helpers import mint_test_access

    access = await mint_test_access(user=user)
    me = await n07.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert {g["name"] for g in me.json()["groups"]} == {"Team-A"}
    exchanged = await n07.exchange(await n07.login(provider, sub="s-session", groups=[]))
    assert exchanged.status_code == 200
    assert REFRESH_TOKEN_COOKIE_NAME in exchanged.cookies
    me = await n07.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert {g["name"] for g in me.json()["groups"]} == set()


@pytest.mark.parametrize("how", ["removed", "moved"])
async def test_the_link_must_still_belong_to_the_account(n07, how):
    provider = await n07.provider({})
    user = await n07.user(f"n07-link-{how}", provider, sub=f"s-link-{how}")
    other = User(username=f"n07-other-{how}", auth_source="oidc", role="user", is_active=True)
    n07.db.add(other)
    token = await n07.login(provider, sub=f"s-link-{how}")
    async with database.async_session() as session:  # raw: no route revokes the token here
        if how == "removed":
            await session.execute(text("DELETE FROM user_oidc_links WHERE user_id = :u"), {"u": user.id})
        else:
            await session.execute(
                text("UPDATE user_oidc_links SET user_id = :o WHERE user_id = :u"), {"o": other.id, "u": user.id}
            )
        await session.commit()
    assert refusal(await n07.exchange(token)) == oidc_group_sync.POLICY_CHANGED


# ── the permission boundary (S36, S43): rows flushed, the deadline, one commit ──


class _Clock:
    """``now_utc`` that jumps to the attempt's deadline once a named step has run."""

    def __init__(self, monkeypatch):
        self.deadline = None
        self.jumped = False
        monkeypatch.setattr(oidc_group_sync, "now_utc", self)

    def __call__(self):
        if self.jumped:
            return datetime.fromtimestamp(self.deadline, tz=timezone.utc)
        return datetime.now(timezone.utc)

    def jump_after(self, target, name, monkeypatch):
        original = getattr(target, name)

        async def wrapped(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.jumped = True
            return result

        monkeypatch.setattr(target, name, wrapped)


async def _deadline_of(token):
    return json.loads(
        await fresh_scalar("SELECT oidc_login_evidence FROM auth_ephemeral_tokens WHERE token = :t", t=token)
    )["deadline"]


async def test_the_deadline_reached_inside_the_issuer_rolls_everything_back(n07, monkeypatch):
    from backend.app.api.routes import auth as auth_routes

    a, b = await n07.group("Team-A"), await n07.group("Team-B")
    provider = await n07.provider({"idp-a": a.id, "idp-b": b.id})
    user = await n07.user("n07-boundary-session", provider, sub="s-boundary-session", groups=[a])
    token = await n07.login(provider, sub="s-boundary-session", groups=["idp-b"])
    clock = _Clock(monkeypatch)
    clock.deadline = await _deadline_of(token)
    clock.jump_after(auth_routes, "_issue_refresh_cookie", monkeypatch)
    sessions_before = await fresh_scalar(
        "SELECT count(*) FROM auth_sessions WHERE account_identity = :a", a=user.account_identity
    )

    response = await n07.exchange(token)
    assert refusal(response) == oidc_group_sync.LOGIN_EXPIRED
    assert clock.jumped  # the issuer really staged the session before the boundary refused it
    assert REFRESH_TOKEN_COOKIE_NAME not in response.cookies
    assert await fresh_groups(user.id) == {"Team-A"}
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token = :t", t=token) == 1
    assert (
        await fresh_scalar("SELECT count(*) FROM auth_sessions WHERE account_identity = :a", a=user.account_identity)
        == sessions_before
    )


async def test_the_deadline_reached_inside_the_transfer_publishes_no_pre_auth(n07, monkeypatch):
    provider = await n07.provider({})
    user = await n07.user("n07-boundary-transfer", provider, sub="s-boundary-transfer")
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = pyotp.random_base32()
    n07.db.add(factor)
    token = await n07.login(provider, sub="s-boundary-transfer")
    clock = _Clock(monkeypatch)
    clock.deadline = await _deadline_of(token)
    clock.jump_after(mfa, "create_pre_auth_token", monkeypatch)

    assert refusal(await n07.exchange(token)) == oidc_group_sync.LOGIN_EXPIRED
    assert clock.jumped
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token = :t", t=token) == 1
    assert (
        await fresh_scalar(
            "SELECT count(*) FROM auth_ephemeral_tokens WHERE token_type = 'pre_auth' AND account_identity = :a",
            a=user.account_identity,
        )
        == 0
    )


async def test_the_deadline_reached_at_the_callback_boundary_writes_nothing(n07, monkeypatch):
    provider = await n07.provider({}, auto_create_users=True)
    clock = _Clock(monkeypatch)
    real_build = oidc_group_sync.build_evidence

    def build_then_jump(**kwargs):
        clock.deadline = kwargs["deadline"]
        clock.jumped = True
        return real_build(**kwargs)

    monkeypatch.setattr(oidc_group_sync, "build_evidence", build_then_jump)
    location = await n07.callback(provider, sub="s-boundary-callback", email="cb@example.invalid")
    assert error_code(location) == "token_validation_failed"
    assert (
        await fresh_scalar("SELECT count(*) FROM user_oidc_links WHERE provider_user_id = 's-boundary-callback'") == 0
    )
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token_type = 'oidc_exchange'") == 0


async def test_an_abandoned_default_administrator_cannot_cover_the_last_admins_demotion(n07):
    admins = await n07.group("Administrators")
    provider = await n07.provider({}, auto_create_users=True, default_group_id=admins.id)
    await n07.login(provider, sub="s-abandoned-cover")  # never exchanged: an account without groups
    admin_id = await fresh_scalar("SELECT id FROM users WHERE username = 'test_admin'")
    removed = await n07.client.delete(f"/api/v1/groups/{admins.id}/users/{admin_id}")
    assert removed.status_code == 400, removed.text  # still the last active administrator
    assert "Administrators" in await fresh_groups(admin_id)


# ── deadlines at the side effects (S26, S33) ─────────────────────────────────


async def _two_fa_attempts(username):
    return await fresh_scalar(
        "SELECT count(*) FROM auth_rate_limit_events WHERE username = :u AND event_type = '2fa_attempt'", u=username
    )


async def test_a_wrong_factor_after_the_deadline_records_no_failed_attempt(n07, monkeypatch):
    provider = await n07.provider({})
    user = await n07.user("n07-s26", provider, sub="s-s26")
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = pyotp.random_base32()
    n07.db.add(factor)
    exchanged = await n07.exchange(await n07.login(provider, sub="s-s26"))
    pre_auth = exchanged.json()["pre_auth_token"]
    clock = _Clock(monkeypatch)
    clock.deadline = await _deadline_of(pre_auth)
    clock.jump_after(mfa, "check_rate_limit", monkeypatch)  # after the first check, before the factor re-read

    response = await n07.client.post(
        "/api/v1/auth/2fa/verify", json={"pre_auth_token": pre_auth, "code": "000000", "method": "totp"}
    )
    assert refusal(response) == oidc_group_sync.LOGIN_EXPIRED
    assert clock.jumped
    assert await _two_fa_attempts("n07-s26") == 0
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token = :t", t=pre_auth) == 1


async def test_a_wrong_factor_with_live_evidence_is_counted_as_today(n07):
    provider = await n07.provider({})
    user = await n07.user("n07-s26-control", provider, sub="s-s26-control")
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = pyotp.random_base32()
    n07.db.add(factor)
    exchanged = await n07.exchange(await n07.login(provider, sub="s-s26-control"))
    response = await n07.client.post(
        "/api/v1/auth/2fa/verify",
        json={"pre_auth_token": exchanged.json()["pre_auth_token"], "code": "000000", "method": "totp"},
    )
    assert response.status_code == 401
    assert await _two_fa_attempts("n07-s26-control") == 1


async def _email_attempt(n07, monkeypatch, username, sub):
    provider = await n07.provider({})
    user = await n07.user(username, provider, sub=sub, email=f"{username}@example.invalid")
    await mfa._set_email_2fa_enabled(n07.db, user.id, True)
    sent = []
    monkeypatch.setattr(mfa, "get_smtp_settings", AsyncMock(return_value={"host": "offline"}))
    monkeypatch.setattr(mfa, "send_email", lambda **kwargs: sent.append(kwargs))
    exchanged = await n07.exchange(await n07.login(provider, sub=sub))
    return user, exchanged.json()["pre_auth_token"], sent


async def test_the_deadline_before_the_mail_sends_nothing(n07, monkeypatch):
    user, pre_auth, sent = await _email_attempt(n07, monkeypatch, "n07-s33-before", "s-s33-before")
    clock = _Clock(monkeypatch)
    clock.deadline = await _deadline_of(pre_auth)
    clock.jump_after(mfa, "hash_password_async", monkeypatch)

    response = await n07.client.post("/api/v1/auth/2fa/email/send", json={"pre_auth_token": pre_auth})
    assert refusal(response) == oidc_group_sync.LOGIN_EXPIRED
    assert sent == []
    assert await fresh_scalar("SELECT count(*) FROM user_otp_codes WHERE user_id = :u", u=user.id) == 0


async def test_the_deadline_after_an_allowed_mail_publishes_nothing(n07, monkeypatch):
    user, pre_auth, sent = await _email_attempt(n07, monkeypatch, "n07-s33-after", "s-s33-after")
    clock = _Clock(monkeypatch)
    clock.deadline = await _deadline_of(pre_auth)

    def send_then_jump(**kwargs):
        sent.append(kwargs)
        clock.jumped = True

    monkeypatch.setattr(mfa, "send_email", send_then_jump)
    response = await n07.client.post("/api/v1/auth/2fa/email/send", json={"pre_auth_token": pre_auth})
    assert refusal(response) == oidc_group_sync.LOGIN_EXPIRED
    assert len(sent) == 1
    assert await fresh_scalar("SELECT count(*) FROM user_otp_codes WHERE user_id = :u", u=user.id) == 0
    assert (
        await fresh_scalar(
            "SELECT count(*) FROM auth_rate_limit_events WHERE username = :u AND event_type = 'email_send'",
            u="n07-s33-after",
        )
        == 0
    )
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token = :t", t=pre_auth) == 1


async def test_an_old_exchange_with_2fa_is_refused_before_the_factor(n07):
    from backend.app.core.auth_lifetime import binding, capture_account

    provider = await n07.provider({})
    user = await n07.user("n07-legacy-2fa", provider, sub="s-legacy-2fa")
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = pyotp.random_base32()
    n07.db.add(factor)
    n07.db.add(
        AuthEphemeralToken(
            token="legacy-exchange-2fa",
            token_type=TokenType.OIDC_EXCHANGE,
            auth_source="oidc",
            username=user.username,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            **binding(capture_account(user)),
        )
    )
    await n07.db.commit()
    assert refusal(await n07.exchange("legacy-exchange-2fa")) == oidc_group_sync.LOGIN_EXPIRED
    assert (
        await fresh_scalar(
            "SELECT count(*) FROM auth_ephemeral_tokens WHERE token_type = 'pre_auth' AND account_identity = :a",
            a=user.account_identity,
        )
        == 0
    )


async def test_the_deadline_at_the_resend_boundary_publishes_nothing(n07, monkeypatch):
    user, pre_auth, sent = await _email_attempt(n07, monkeypatch, "n07-s36-resend", "s-s36-resend")
    clock = _Clock(monkeypatch)
    clock.deadline = await _deadline_of(pre_auth)
    clock.jump_after(mfa, "create_pre_auth_token", monkeypatch)  # the new pre-auth is staged, not committed

    response = await n07.client.post("/api/v1/auth/2fa/email/send", json={"pre_auth_token": pre_auth})
    assert refusal(response) == oidc_group_sync.LOGIN_EXPIRED
    assert clock.jumped and len(sent) == 1
    assert await fresh_scalar("SELECT count(*) FROM user_otp_codes WHERE user_id = :u", u=user.id) == 0
    assert (
        await fresh_scalar(
            "SELECT count(*) FROM auth_rate_limit_events WHERE username = :u AND event_type = 'email_send'",
            u="n07-s36-resend",
        )
        == 0
    )
    assert (
        await fresh_scalar(
            "SELECT count(*) FROM auth_ephemeral_tokens WHERE token_type = 'pre_auth' AND account_identity = :a",
            a=user.account_identity,
        )
        == 1
    )  # only the old one, back after the rollback


# ── acceptance r1 (N07-A01, A02) and the remaining T2 pairs ──────────────────


async def test_a_null_claim_names_refuses_the_callback(n07):
    a = await n07.group("Team-A")
    provider = await n07.provider({"idp-a": a.id})
    user = await n07.user("n07-a01", provider, sub="s-a01", groups=[a])
    location = await n07.callback(provider, sub="s-a01", _claim_names=None)
    assert error_code(location) == oidc_group_sync.CLAIMS_INVALID
    assert await fresh_groups(user.id) == {"Team-A"}


@pytest.mark.parametrize("two_factor", [False, True])
async def test_evidence_without_the_groups_key_is_refused(n07, two_factor):
    provider = await n07.provider({})
    user = await n07.user(f"n07-a02-{two_factor}", provider, sub=f"s-a02-{two_factor}")
    if two_factor:
        factor = UserTOTP(user_id=user.id, is_enabled=True)
        factor.secret = pyotp.random_base32()
        n07.db.add(factor)
    token = await n07.login(provider, sub=f"s-a02-{two_factor}")
    async with database.async_session() as session:
        row = (await session.execute(select(AuthEphemeralToken).where(AuthEphemeralToken.token == token))).scalar_one()
        row.oidc_login_evidence = {k: v for k, v in row.oidc_login_evidence.items() if k != "groups"}
        await session.commit()
    assert refusal(await n07.exchange(token)) == oidc_group_sync.LOGIN_EXPIRED
    pending = await fresh_scalar(
        "SELECT count(*) FROM auth_ephemeral_tokens WHERE token_type = 'pre_auth' AND account_identity = :a",
        a=user.account_identity,
    )
    assert pending == 0


async def test_two_logins_of_one_user_the_last_completed_wins(n07):
    a, b = await n07.group("Team-A"), await n07.group("Team-B")
    provider = await n07.provider({"idp-a": a.id, "idp-b": b.id})
    user = await n07.user("n07-race", provider, sub="s-race")
    first = await n07.login(provider, sub="s-race", groups=["idp-a"])
    second = await n07.login(provider, sub="s-race", groups=["idp-b"])
    assert (await n07.exchange(second)).status_code == 200
    assert (await n07.exchange(first)).status_code == 200  # completes last
    assert await fresh_groups(user.id) == {"Team-A"}


async def test_an_auto_link_candidate_deactivated_before_its_guard_is_not_linked(n07, monkeypatch):
    target = User(username="n07-deact", auth_source="local", role="user", is_active=True, email="deact@example.invalid")
    n07.db.add(target)
    provider = await n07.provider({}, auto_link_existing_accounts=True, email_claim="preferred_username")
    await n07.db.commit()
    real_guard = mfa.guard_accounts
    fired = []

    async def deactivate_then_guard(db, captures, **kwargs):
        if not fired:
            fired.append(1)
            async with database.async_session() as session:
                await session.execute(text("UPDATE users SET is_active = 0 WHERE id = :i"), {"i": target.id})
                await session.commit()
        return await real_guard(db, captures, **kwargs)

    monkeypatch.setattr(mfa, "guard_accounts", deactivate_then_guard)
    authorize = await n07.client.get(f"/api/v1/auth/oidc/authorize/{provider.id}")
    query = parse_qs(urlsplit(authorize.json()["auth_url"]).query)
    n07.idp.nonce = query["nonce"][0]
    n07.idp.claims = {"sub": "s-deact", "preferred_username": "deact@example.invalid"}
    callback = await n07.client.get(
        "/api/v1/auth/oidc/callback", params={"state": query["state"][0], "code": "c"}, follow_redirects=False
    )
    # The account guard refuses the deactivated account as it does for any credential.
    assert callback.status_code in (302, 401) and "#oidc_token=" not in callback.headers.get("location", "")
    assert await fresh_scalar("SELECT count(*) FROM user_oidc_links WHERE user_id = :u", u=target.id) == 0
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token_type = 'oidc_exchange'") == 0


async def test_the_deadline_inside_verify_rolls_back_the_factor_and_the_rights(n07, monkeypatch):
    from backend.app.api.routes import auth as auth_routes

    a, b = await n07.group("Team-A"), await n07.group("Team-B")
    provider = await n07.provider({"idp-a": a.id, "idp-b": b.id})
    user = await n07.user("n07-verify-boundary", provider, sub="s-verify-boundary", groups=[a])
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = pyotp.random_base32()
    factor.backup_code_hashes = [mfa.pwd_context.hash("BOUNDRY1")]
    n07.db.add(factor)
    exchanged = await n07.exchange(await n07.login(provider, sub="s-verify-boundary", groups=["idp-b"]))
    pre_auth = exchanged.json()["pre_auth_token"]
    clock = _Clock(monkeypatch)
    clock.deadline = await _deadline_of(pre_auth)
    clock.jump_after(auth_routes, "_issue_refresh_cookie", monkeypatch)
    response = await n07.client.post(
        "/api/v1/auth/2fa/verify", json={"pre_auth_token": pre_auth, "code": "BOUNDRY1", "method": "backup"}
    )
    assert refusal(response) == oidc_group_sync.LOGIN_EXPIRED
    assert clock.jumped
    assert await fresh_groups(user.id) == {"Team-A"}
    assert await fresh_scalar("SELECT count(*) FROM auth_ephemeral_tokens WHERE token = :t", t=pre_auth) == 1
    async with database.async_session() as session:
        kept = (await session.execute(select(UserTOTP).where(UserTOTP.user_id == user.id))).scalar_one()
        assert len(kept.backup_code_hashes) == 1  # the backup code was not spent


async def test_a_local_two_factor_login_keeps_a_sql_null_evidence(n07):
    from backend.app.core.auth import get_password_hash

    user = User(
        username="n07-local-2fa",
        password_hash=get_password_hash("LocalPass1!x"),
        role="user",
        is_active=True,
        email="local2fa@example.invalid",
    )
    n07.db.add(user)
    await n07.db.flush()
    factor = UserTOTP(user_id=user.id, is_enabled=True)
    factor.secret = pyotp.random_base32()
    n07.db.add(factor)
    await n07.db.commit()
    login = await n07.client.post("/api/v1/auth/login", json={"username": "n07-local-2fa", "password": "LocalPass1!x"})
    assert login.status_code == 200 and login.json()["requires_2fa"], login.text
    pre_auth = login.json()["pre_auth_token"]
    null_rows = await fresh_scalar(
        "SELECT count(*) FROM auth_ephemeral_tokens WHERE token = :t AND oidc_login_evidence IS NULL", t=pre_auth
    )
    assert null_rows == 1
    verified = await n07.client.post(
        "/api/v1/auth/2fa/verify",
        json={"pre_auth_token": pre_auth, "code": pyotp.TOTP(factor.secret).now(), "method": "totp"},
    )
    assert verified.status_code == 200, verified.text
