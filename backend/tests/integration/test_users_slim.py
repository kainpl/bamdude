"""``GET /users/slim`` — id and username, and nothing else (upstream #1894).

Archives, the queue and statistics report ownership as a numeric
``created_by_id``, and statistics accept it as a filter. Nothing let a caller
discover whose id was whose: the only user listing returns emails, roles, group
membership and full permission sets, so it is administrative and rejects both
API keys and any group that was not handed ``users:read``. An operator granted
``stats:filter_by_user`` but not ``users:read`` got an empty filter with no
indication why.

⚠️ The new permission grants **no data a key could not already reach**: for
API-keyed requests the permission deps return None as ``current_user``, so the
user-filter guard short-circuits and ``?created_by_id=N`` is already honoured
for every N. What was missing was the ability to address the filter, not
permission to use it.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.core.auth import generate_api_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.user import User

pytestmark = pytest.mark.integration


async def _user(db_session, username: str, permissions: list[str]) -> User:
    group = Group(name=f"grp-{username}", description="test", permissions=permissions)
    db_session.add(group)
    await db_session.flush()
    user = User(username=username, email=f"{username}@example.com", password_hash="x", role="user")
    user.groups.append(group)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _key(db_session, *, owner: User | None = None, **flags) -> str:
    raw, key_hash, key_prefix = generate_api_key()
    db_session.add(
        APIKey(
            name=f"k-{key_prefix}",
            key_hash=key_hash,
            key_prefix=key_prefix,
            enabled=True,
            user_id=owner.id if owner else None,
            **flags,
        )
    )
    await db_session.commit()
    return raw


class TestTheShape:
    @pytest.mark.asyncio
    async def test_it_returns_only_id_and_username(self, async_client: AsyncClient, db_session):
        """⚠️ This shape IS the contract. Every field added here widens what
        every can_read_status API key can read about every account."""
        await _user(db_session, "someone", [Permission.STATS_READ.value])

        response = await async_client.get("/api/v1/users/slim")

        assert response.status_code == 200, response.text
        rows = response.json()
        assert rows, "the seeded admin alone would make this non-empty"
        for row in rows:
            assert set(row) == {"id", "username"}

    @pytest.mark.asyncio
    async def test_it_is_ordered_by_name(self, async_client: AsyncClient, db_session):
        await _user(db_session, "zoe", [Permission.STATS_READ.value])
        await _user(db_session, "aaron", [Permission.STATS_READ.value])

        names = [row["username"] for row in (await async_client.get("/api/v1/users/slim")).json()]

        assert names == sorted(names)

    @pytest.mark.asyncio
    async def test_slim_is_not_parsed_as_a_user_id(self, async_client: AsyncClient):
        """⚠️ FastAPI matches in declaration order, so this route has to be
        declared before ``/{user_id}`` — the reverse order answers 422."""
        response = await async_client.get("/api/v1/users/slim")

        assert response.status_code != 422


class TestWhoMayRead:
    @pytest.mark.asyncio
    async def test_an_api_key_can(self, async_client: AsyncClient, db_session):
        owner = await _user(db_session, "keyholder", [Permission.USERS_READ_SLIM.value])
        raw = await _key(db_session, owner=owner, can_read_status=True)

        del async_client.headers["Authorization"]
        response = await async_client.get("/api/v1/users/slim", headers={"X-API-Key": raw})

        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_the_same_key_is_still_refused_the_full_listing(self, async_client: AsyncClient, db_session):
        """The point of splitting the permission: ``users:read`` is unmapped for
        keys — administrative — and stays that way."""
        owner = await _user(db_session, "keyholder2", [p.value for p in Permission])
        raw = await _key(db_session, owner=owner, can_read_status=True)

        del async_client.headers["Authorization"]
        response = await async_client.get("/api/v1/users/", headers={"X-API-Key": raw})

        assert response.status_code == 403, response.text

    @pytest.mark.asyncio
    async def test_a_key_without_read_status_cannot(self, async_client: AsyncClient, db_session):
        owner = await _user(db_session, "blind", [Permission.USERS_READ_SLIM.value])
        raw = await _key(db_session, owner=owner, can_read_status=False)

        del async_client.headers["Authorization"]
        response = await async_client.get("/api/v1/users/slim", headers={"X-API-Key": raw})

        assert response.status_code == 403, response.text

    @pytest.mark.asyncio
    async def test_users_read_alone_still_works(self, async_client: AsyncClient, db_session):
        """A group holding the broader permission must keep working without a
        backfill — otherwise this "narrowing" would be a regression for every
        admin group that predates it."""
        from backend.app.core.auth import create_access_token

        await _user(db_session, "olderadmin", [Permission.USERS_READ.value])
        token = create_access_token(data={"sub": "olderadmin"})

        response = await async_client.get("/api/v1/users/slim", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200, response.text


class TestTheMigrationSeedsIt:
    def test_the_permission_is_named_in_the_migration(self):
        """The O2 discipline: Administrators are not self-healed at startup, so
        a new Permission that never lands in a migration is missing forever on
        every upgraded install."""
        from backend.app.migrations import m145_users_read_slim_permission as m

        assert [Permission.USERS_READ_SLIM.value] == m.NEW_PERMISSIONS
        assert m.version == 145

    def test_operators_hold_it_by_default(self):
        """They hold ``stats:filter_by_user``, which is what makes the mapping
        worth having."""
        from backend.app.core.permissions import DEFAULT_GROUPS

        assert Permission.USERS_READ_SLIM.value in DEFAULT_GROUPS["Operators"]["permissions"]

    def test_viewers_do_not(self):
        """They can read stats but not filter them by user, so the listing would
        answer a question they cannot ask."""
        from backend.app.core.permissions import DEFAULT_GROUPS

        assert Permission.USERS_READ_SLIM.value not in DEFAULT_GROUPS["Viewers"]["permissions"]
