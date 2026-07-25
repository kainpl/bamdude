"""Overlay-token scope isolation (upstream #2613).

The streaming overlay renders without a login, but everything it draws is
auth-gated, so OBS — a fresh browser with no session — saw a blank page. The fix
gives it a long-lived token, and the interesting part is the *boundary*:

``overlay`` is a separate grant, not a widening of ``camera_stream``. The overlay
status feed names the file being printed; ``camera_stream`` tokens are already in
the wild, minted to hand out video alone. Folding the feed into that scope would
silently give every existing token the ability to read what each printer is
making. These tests pin that in both directions, plus the one deliberate overlap:
an overlay token *may* pull the video its own view shows.
"""

import pytest

from backend.app.models.user import User
from backend.app.services.long_lived_tokens import (
    ALLOWED_SCOPES,
    STREAM_SCOPES,
    create_token,
    verify_token,
)


@pytest.fixture
async def alice(db_session) -> User:
    user = User(
        username="alice-overlay-scope",
        email="alice-overlay-scope@example.test",
        password_hash="x",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


class TestScopeRegistry:
    def test_allowed_scopes_is_an_exact_set(self):
        """A new scope must be added deliberately, never by accident."""
        assert set(ALLOWED_SCOPES) == {"camera_stream", "camwall", "overlay"}

    def test_stream_scopes_is_an_explicit_allowlist(self):
        """The stream gate names its scopes — it is never "any scope"."""
        assert set(STREAM_SCOPES) == {"camera_stream", "camwall", "overlay"}
        assert set(STREAM_SCOPES) <= set(ALLOWED_SCOPES)


class TestScopeIsolation:
    @pytest.mark.asyncio
    async def test_camera_stream_token_cannot_reach_the_overlay_feed(self, db_session, alice):
        """The load-bearing one: a video token must not read the print filename."""
        created = await create_token(
            db_session, user_id=alice.id, name="ha-card", expires_in_days=7, scope="camera_stream"
        )
        assert await verify_token(db_session, created.plaintext, scope="overlay") is None

    @pytest.mark.asyncio
    async def test_overlay_token_is_not_accepted_as_a_camera_stream_only_token(self, db_session, alice):
        """Scopes don't imply each other in either direction."""
        created = await create_token(db_session, user_id=alice.id, name="obs", expires_in_days=7, scope="overlay")
        assert await verify_token(db_session, created.plaintext, scope="camera_stream") is None

    @pytest.mark.asyncio
    async def test_overlay_token_passes_the_stream_gate(self, db_session, alice):
        """The one deliberate overlap: the overlay has to fetch its own video."""
        created = await create_token(db_session, user_id=alice.id, name="obs", expires_in_days=7, scope="overlay")
        assert await verify_token(db_session, created.plaintext, scope=STREAM_SCOPES) is not None

    @pytest.mark.asyncio
    async def test_camera_stream_token_still_passes_the_stream_gate(self, db_session, alice):
        """Widening the gate to a collection must not break the original scope."""
        created = await create_token(
            db_session, user_id=alice.id, name="frigate", expires_in_days=7, scope="camera_stream"
        )
        assert await verify_token(db_session, created.plaintext, scope=STREAM_SCOPES) is not None

    @pytest.mark.asyncio
    async def test_camwall_token_cannot_reach_the_overlay_feed(self, db_session, alice):
        """The wall is trusted never to name the part; the overlay names it.

        Folding overlay into camwall would silently widen every wall token
        already pinned to a lobby screen.
        """
        created = await create_token(db_session, user_id=alice.id, name="lobby-tv", expires_in_days=7, scope="camwall")
        assert await verify_token(db_session, created.plaintext, scope="overlay") is None

    @pytest.mark.asyncio
    async def test_camera_stream_token_cannot_enumerate_the_fleet(self, db_session, alice):
        """A token minted to hand out video must not gain the printer list."""
        created = await create_token(
            db_session, user_id=alice.id, name="ha-card", expires_in_days=7, scope="camera_stream"
        )
        assert await verify_token(db_session, created.plaintext, scope="camwall") is None

    @pytest.mark.asyncio
    async def test_each_token_matches_its_own_scope(self, db_session, alice):
        for scope in ("camera_stream", "camwall", "overlay"):
            created = await create_token(
                db_session, user_id=alice.id, name=f"t-{scope}", expires_in_days=7, scope=scope
            )
            assert await verify_token(db_session, created.plaintext, scope=scope) is not None
