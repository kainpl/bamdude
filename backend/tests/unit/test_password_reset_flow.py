"""Self-service password recovery: the token half.

The flow this replaced generated a password at REQUEST time, stored it and
mailed it in the clear. Two things were wrong with that, and both are what
these tests pin:

* knowing an address was enough to rotate that account's password — the owner
  was locked out by a request they never made and never saw;
* the password travelled by e-mail, so anyone who ever read the mailbox (or the
  mail server's spool) had it.

Now the account is untouched until somebody proves they read the message, by
spending a single-use token whose hash is all the database holds.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.api.routes.auth import (
    PASSWORD_RESET_TOKEN_TTL,
    consume_password_reset_token,
    create_password_reset_token,
    is_password_reset_available,
)
from backend.app.core.auth import get_password_hash, hash_client_secret, verify_password
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType
from backend.app.models.user import User


async def _make_user(db, username="alice", password="OldPassw0rd", **kwargs):
    user = User(
        username=username,
        email=kwargs.pop("email", f"{username}@example.com"),
        password_hash=get_password_hash(password),
        role=kwargs.pop("role", "user"),
        is_active=kwargs.pop("is_active", True),
        **kwargs,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


class TestTheToken:
    async def test_a_fresh_token_names_its_account(self, db_session):
        raw = await create_password_reset_token(db_session, "alice")
        assert await consume_password_reset_token(db_session, raw) == "alice"

    async def test_a_token_can_be_spent_only_once(self, db_session):
        """Mail clients pre-fetch links and messages get forwarded. A token that
        survived its own first use would be spendable by whoever else has it."""
        raw = await create_password_reset_token(db_session, "alice")
        assert await consume_password_reset_token(db_session, raw) == "alice"
        assert await consume_password_reset_token(db_session, raw) is None

    async def test_the_database_never_holds_the_raw_token(self, db_session):
        """A stolen database must not be a set of working reset links."""
        raw = await create_password_reset_token(db_session, "alice")
        stored = (
            (
                await db_session.execute(
                    select(AuthEphemeralToken.token).where(AuthEphemeralToken.token_type == TokenType.PASSWORD_RESET)
                )
            )
            .scalars()
            .all()
        )
        assert stored == [hash_client_secret(raw)]
        assert raw not in stored

    async def test_asking_again_retires_the_previous_link(self, db_session):
        """Two live links for one account means the older mail still works after
        the user has already used the newer one."""
        first = await create_password_reset_token(db_session, "alice")
        second = await create_password_reset_token(db_session, "alice")
        assert await consume_password_reset_token(db_session, first) is None
        assert await consume_password_reset_token(db_session, second) == "alice"

    async def test_an_expired_token_is_refused(self, db_session):
        raw = await create_password_reset_token(db_session, "alice")
        row = (
            await db_session.execute(
                select(AuthEphemeralToken).where(AuthEphemeralToken.token == hash_client_secret(raw))
            )
        ).scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.commit()
        assert await consume_password_reset_token(db_session, raw) is None

    async def test_a_token_for_someone_else_does_not_open_this_account(self, db_session):
        await create_password_reset_token(db_session, "alice")
        bob = await create_password_reset_token(db_session, "bob")
        assert await consume_password_reset_token(db_session, bob) == "bob"

    async def test_garbage_is_refused_without_blowing_up(self, db_session):
        assert await consume_password_reset_token(db_session, "not-a-token") is None

    def test_the_link_does_not_outlive_the_hour(self):
        """Long enough for a slow mail server and a busy person; short enough
        that a link left in an inbox is not a standing key to the account."""
        assert timedelta(hours=1) == PASSWORD_RESET_TOKEN_TTL


class TestWhetherRecoveryIsOffered:
    """⚠️ Not gated on ``advanced_auth_enabled``. That setting bundles generated
    passwords, login-by-email and notification mail; tying recovery to it meant
    an operator who had configured SMTP and tested it still got a 400 while the
    login page went on offering the link."""

    def test_smtp_and_local_login_are_the_two_conditions(self):
        assert is_password_reset_available(smtp_configured=True, local_login_enabled=True)

    def test_without_smtp_there_is_nothing_to_send(self):
        assert not is_password_reset_available(smtp_configured=False, local_login_enabled=True)

    def test_without_local_login_there_is_no_password_worth_resetting(self):
        assert not is_password_reset_available(smtp_configured=True, local_login_enabled=False)


class TestConfirming:
    async def test_the_new_password_replaces_the_old_one(self, db_session, async_client):
        user = await _make_user(db_session)
        raw = await create_password_reset_token(db_session, user.username)

        response = await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": raw, "new_password": "BrandNew1"},
        )
        assert response.status_code == 200, response.text

        await db_session.refresh(user)
        assert verify_password("BrandNew1", user.password_hash)
        assert not verify_password("OldPassw0rd", user.password_hash)

    async def test_it_stamps_password_changed_at_so_old_tokens_die(self, db_session, async_client):
        """§18.4: an access token minted before the reset must stop being fresh.
        Otherwise whoever prompted the reset keeps their session."""
        user = await _make_user(db_session, username="carol")
        assert user.password_changed_at is None
        raw = await create_password_reset_token(db_session, user.username)

        await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": raw, "new_password": "BrandNew1"},
        )
        await db_session.refresh(user)
        assert user.password_changed_at is not None

    async def test_the_same_link_cannot_be_used_twice(self, db_session, async_client):
        user = await _make_user(db_session, username="dave")
        raw = await create_password_reset_token(db_session, user.username)

        first = await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": raw, "new_password": "BrandNew1"},
        )
        second = await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": raw, "new_password": "Different2"},
        )
        assert first.status_code == 200
        assert second.status_code == 400
        await db_session.refresh(user)
        assert verify_password("BrandNew1", user.password_hash)

    async def test_an_unknown_token_is_refused(self, async_client):
        response = await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": "made-up", "new_password": "BrandNew1"},
        )
        assert response.status_code == 400

    async def test_the_new_password_must_satisfy_the_ordinary_rules(self, db_session, async_client):
        """The reset path is not a way around the complexity rules — an account
        recovered with 'short' could never set that password again through the
        UI that just accepted it."""
        user = await _make_user(db_session, username="erin")
        raw = await create_password_reset_token(db_session, user.username)

        response = await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": raw, "new_password": "short"},
        )
        assert response.status_code == 422
        await db_session.refresh(user)
        assert verify_password("OldPassw0rd", user.password_hash)

    async def test_a_deactivated_account_is_not_reopened_by_a_link(self, db_session, async_client):
        user = await _make_user(db_session, username="frank", is_active=False)
        raw = await create_password_reset_token(db_session, user.username)

        response = await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": raw, "new_password": "BrandNew1"},
        )
        assert response.status_code == 400
        await db_session.refresh(user)
        assert verify_password("OldPassw0rd", user.password_hash)

    async def test_the_endpoint_is_reachable_without_a_token_of_any_kind(self, async_client):
        """⚠️ The regression this exists for: it is reached by somebody who
        cannot sign in. Behind the auth gate it would answer 401 to the only
        person who would ever click the link."""
        response = await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": "made-up", "new_password": "BrandNew1"},
        )
        assert response.status_code != 401
        assert response.status_code != 503


class TestRequesting:
    """The half that sends the mail. ⚠️ It must not touch the account."""

    @staticmethod
    def _smtp_configured(monkeypatch):
        from unittest.mock import MagicMock

        from backend.app.schemas.auth import SMTPSettings

        sent = []
        settings = SMTPSettings(
            smtp_host="mail.example.com",
            smtp_port=587,
            smtp_username="bot",
            smtp_password="secret",
            smtp_from_email="bamdude@example.com",
        )

        async def _get_smtp_settings(_db):
            return settings

        monkeypatch.setattr("backend.app.api.routes.auth.get_smtp_settings", _get_smtp_settings)
        monkeypatch.setattr(
            "backend.app.api.routes.auth.send_email",
            MagicMock(side_effect=lambda *args, **kwargs: sent.append(args)),
        )
        return sent

    async def test_asking_for_a_reset_does_not_change_the_password(self, db_session, async_client, monkeypatch):
        """⚠️ The regression this whole rewrite exists for. The old flow set a
        new password here, so knowing somebody's address was enough to lock
        them out of an account they were happily using."""
        self._smtp_configured(monkeypatch)
        user = await _make_user(db_session, username="grace")

        response = await async_client.post("/api/v1/auth/forgot-password", json={"email": user.email})
        assert response.status_code == 200, response.text

        await db_session.refresh(user)
        assert verify_password("OldPassw0rd", user.password_hash)
        assert user.password_changed_at is None

    async def test_the_mail_carries_a_link_and_never_a_password(self, db_session, async_client, monkeypatch):
        sent = self._smtp_configured(monkeypatch)
        user = await _make_user(db_session, username="henry")

        await async_client.post("/api/v1/auth/forgot-password", json={"email": user.email})

        assert len(sent) == 1
        _settings, recipient, _subject, text_body, _html = sent[0]
        assert recipient == user.email
        assert "#reset_token=" in text_body
        # Whatever the template says, the account's actual password is not in it.
        assert "OldPassw0rd" not in text_body

    async def test_the_link_it_sends_actually_works(self, db_session, async_client, monkeypatch):
        """End to end: the token in the mail is the token the confirm half
        accepts. The two halves were written against each other's assumptions
        once already, and the result was a step posting to a route that did
        not exist."""
        sent = self._smtp_configured(monkeypatch)
        user = await _make_user(db_session, username="ivy")

        await async_client.post("/api/v1/auth/forgot-password", json={"email": user.email})
        text_body = sent[0][3]
        token = text_body.split("#reset_token=")[1].split()[0]

        response = await async_client.post(
            "/api/v1/auth/forgot-password/confirm",
            json={"token": token, "new_password": "BrandNew1"},
        )
        assert response.status_code == 200, response.text
        await db_session.refresh(user)
        assert verify_password("BrandNew1", user.password_hash)

    async def test_an_unknown_address_is_answered_the_same_way(self, async_client, monkeypatch):
        """Anti-enumeration: the reply must not say whether the account exists."""
        sent = self._smtp_configured(monkeypatch)
        response = await async_client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"})
        assert response.status_code == 200
        assert sent == []
