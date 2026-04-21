"""Integration tests for Authentication API endpoints.

Tests the full request/response cycle for /api/v1/auth/ and /api/v1/users/ endpoints.

Note: the ``async_client`` fixture seeds a ``test_admin`` user and attaches its
JWT by default (see ``backend/tests/conftest.py``), so every request from this
file already carries admin credentials unless the test overrides them.
"""

import pytest
from httpx import ASGITransport, AsyncClient


class TestAuthStatusAPI:
    """Integration tests for /api/v1/auth/status endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_auth_status_after_setup(self, async_client: AsyncClient):
        """Status reflects the pre-seeded admin: setup complete, auth on."""
        response = await async_client.get("/api/v1/auth/status")

        assert response.status_code == 200
        result = response.json()
        assert result["auth_enabled"] is True  # legacy field, always true now
        assert result["requires_setup"] is False


class TestAuthSetupAPI:
    """Integration tests for /api/v1/auth/setup endpoint.

    The setup endpoint only opens while no admin exists. Since ``async_client``
    seeds an admin, these tests hit /setup expecting the "already set up"
    rejection path.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_setup_rejected_when_admin_exists(self, async_client: AsyncClient):
        """Setup returns 403 once any admin user is in the database."""
        response = await async_client.post(
            "/api/v1/auth/setup",
            json={
                "admin_username": "another_admin",
                "admin_password": "anotherpass123",
            },
        )

        assert response.status_code == 403
        assert "already" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_setup_requires_credentials(self, async_client: AsyncClient):
        """Setup rejects requests missing the required fields (schema validation)."""
        response = await async_client.post("/api/v1/auth/setup", json={})
        # Pydantic returns 422 for schema-level validation errors before the
        # endpoint body ever runs.
        assert response.status_code == 422


class TestAuthSetupBootstrap:
    """Setup tests that require a fresh DB with no admin yet.

    Uses its own AsyncClient so we can clear the ``test_admin`` user and
    exercise the bootstrap path that the normal ``async_client`` fixture hides.
    """

    @pytest.fixture
    async def fresh_client(self, async_client, test_engine):
        """Wipe the pre-seeded admin and return a client with no auth header."""
        from sqlalchemy import delete
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from backend.app.main import app, invalidate_setup_gate_cache
        from backend.app.models.group import user_groups
        from backend.app.models.settings import Settings
        from backend.app.models.user import User

        test_session = async_sessionmaker(test_engine, expire_on_commit=False)
        async with test_session() as db:
            # Drop the association rows first, then the user itself - SQLite
            # in-memory doesn't enforce FK CASCADE by default, so we do it
            # explicitly instead of relying on ORM cascade.
            await db.execute(delete(user_groups))
            await db.execute(delete(User).where(User.username == "test_admin"))
            await db.execute(delete(Settings).where(Settings.key.in_(["setup_completed", "auth_enabled"])))
            await db.commit()
        invalidate_setup_gate_cache()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_requires_setup_when_no_admin(self, fresh_client: AsyncClient):
        """/auth/status reports requires_setup=true before any admin exists."""
        response = await fresh_client.get("/api/v1/auth/status")
        assert response.status_code == 200
        assert response.json()["requires_setup"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_setup_creates_admin_and_returns_token(self, fresh_client: AsyncClient):
        """Successful setup creates the admin and returns a usable JWT."""
        response = await fresh_client.post(
            "/api/v1/auth/setup",
            json={
                "admin_username": "bootstrap_admin",
                "admin_password": "bootstrappass123",
                "admin_email": "admin@example.com",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["admin_created"] is True
        assert body["token_type"] == "bearer"
        assert body["access_token"]
        assert body["user"]["username"] == "bootstrap_admin"
        assert body["user"]["is_admin"] is True

        # The returned token must actually work against a protected endpoint.
        me = await fresh_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["username"] == "bootstrap_admin"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_non_whitelisted_endpoint_returns_503_during_setup(self, fresh_client: AsyncClient):
        """The setup gate blocks every non-whitelisted API route until an admin exists."""
        response = await fresh_client.get("/api/v1/printers/")
        assert response.status_code == 503
        assert response.json()["detail"] == "setup_required"


class TestAuthLoginAPI:
    """Integration tests for /api/v1/auth/login endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_login_success(self, async_client: AsyncClient):
        """Login succeeds with valid credentials against the pre-seeded admin."""
        response = await async_client.post(
            "/api/v1/auth/login",
            json={"username": "test_admin", "password": "test_admin_pass"},
        )

        assert response.status_code == 200
        result = response.json()
        assert "access_token" in result
        assert result["token_type"] == "bearer"
        assert result["user"]["username"] == "test_admin"
        assert result["user"]["role"] == "admin"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_login_invalid_credentials(self, async_client: AsyncClient):
        """Login fails with wrong password."""
        response = await async_client.post(
            "/api/v1/auth/login",
            json={"username": "test_admin", "password": "wrongpassword"},
        )

        assert response.status_code == 401
        assert "Incorrect username or password" in response.json()["detail"]


class TestAuthMeAPI:
    """Integration tests for /api/v1/auth/me endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_me_without_token(self, async_client: AsyncClient):
        """Verify /me fails without authentication token."""
        response = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": ""},
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_me_with_valid_token(self, async_client: AsyncClient):
        """Verify /me returns the pre-seeded admin when the default JWT is used."""
        response = await async_client.get("/api/v1/auth/me")

        assert response.status_code == 200
        result = response.json()
        assert result["username"] == "test_admin"
        assert result["role"] == "admin"
        assert result["is_active"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_me_with_api_key_bearer(self, async_client: AsyncClient, db_session):
        """Verify /me returns synthetic admin user when using API key via Bearer token."""
        from backend.app.core.auth import generate_api_key
        from backend.app.models.api_key import APIKey

        # Create an API key directly in the database
        full_key, key_hash, key_prefix = generate_api_key()
        api_key = APIKey(name="test-kiosk", key_hash=key_hash, key_prefix=key_prefix, enabled=True)
        db_session.add(api_key)
        await db_session.commit()

        # Call /me with the API key as Bearer token
        response = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {full_key}"},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["id"] == 0
        assert result["username"].startswith("api-key:")
        assert result["role"] == "admin"
        assert result["is_admin"] is True
        assert result["is_active"] is True
        assert len(result["permissions"]) > 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_me_with_api_key_header(self, async_client: AsyncClient, db_session):
        """Verify /me returns synthetic admin user when using X-API-Key header."""
        from backend.app.core.auth import generate_api_key
        from backend.app.models.api_key import APIKey

        full_key, key_hash, key_prefix = generate_api_key()
        api_key = APIKey(name="test-kiosk-header", key_hash=key_hash, key_prefix=key_prefix, enabled=True)
        db_session.add(api_key)
        await db_session.commit()

        response = await async_client.get(
            "/api/v1/auth/me",
            headers={"X-API-Key": full_key},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["id"] == 0
        assert result["username"].startswith("api-key:")
        assert result["is_admin"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_me_with_invalid_api_key(self, async_client: AsyncClient):
        """Verify /me rejects invalid API key."""
        response = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer bb_invalid_key_value"},
        )

        assert response.status_code == 401


class TestUsersAPI:
    """Integration tests for /api/v1/users/ endpoints."""

    @pytest.fixture
    async def auth_token(self, async_client: AsyncClient):
        """Return a JWT for the pre-seeded admin."""
        from backend.app.core.auth import create_access_token

        return create_access_token(data={"sub": "test_admin"})

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_users_requires_auth(self, async_client: AsyncClient):
        """Verify listing users requires authentication."""
        response = await async_client.get(
            "/api/v1/users/",
            headers={"Authorization": ""},
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_users_as_admin(self, async_client: AsyncClient, auth_token: str):
        """Verify admin can list users."""
        response = await async_client.get(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 200
        result = response.json()
        assert isinstance(result, list)
        assert len(result) >= 1  # At least the admin user

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_user(self, async_client: AsyncClient, auth_token: str):
        """Verify admin can create a new user."""
        response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "username": "newuser",
                "password": "newuserpassword",
                "role": "user",
            },
        )

        assert response.status_code == 201
        result = response.json()
        assert result["username"] == "newuser"
        assert result["role"] == "user"
        assert result["is_active"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_user_duplicate_username(self, async_client: AsyncClient, auth_token: str):
        """Verify creating user with duplicate username fails."""
        # Create first user
        await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "username": "duplicateuser",
                "password": "password123",
                "role": "user",
            },
        )

        # Try to create duplicate
        response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "username": "duplicateuser",
                "password": "password456",
                "role": "user",
            },
        )

        assert response.status_code == 400
        assert "Username already exists" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_user(self, async_client: AsyncClient, auth_token: str):
        """Verify admin can update a user."""
        # Create user
        create_response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "username": "updateuser",
                "password": "password123",
                "role": "user",
            },
        )
        user_id = create_response.json()["id"]

        # Update user
        response = await async_client.patch(
            f"/api/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"role": "admin"},
        )

        assert response.status_code == 200
        assert response.json()["role"] == "admin"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_user(self, async_client: AsyncClient, auth_token: str):
        """Verify admin can delete a user."""
        # Create user
        create_response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "username": "deleteuser",
                "password": "password123",
                "role": "user",
            },
        )
        user_id = create_response.json()["id"]

        # Delete user
        response = await async_client.delete(
            f"/api/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 204


class TestGroupsAPI:
    """Integration tests for /api/v1/groups/ endpoints."""

    @pytest.fixture
    async def auth_token(self, async_client: AsyncClient):
        """Return a JWT for the pre-seeded admin."""
        from backend.app.core.auth import create_access_token

        return create_access_token(data={"sub": "test_admin"})

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_groups(self, async_client: AsyncClient, auth_token: str):
        """Verify listing groups returns default groups."""
        response = await async_client.get(
            "/api/v1/groups/",
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 200
        groups = response.json()
        assert isinstance(groups, list)
        # Should have default groups: Administrators, Operators, Viewers
        group_names = [g["name"] for g in groups]
        assert "Administrators" in group_names
        assert "Operators" in group_names
        assert "Viewers" in group_names

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_permissions(self, async_client: AsyncClient, auth_token: str):
        """Verify getting available permissions."""
        response = await async_client.get(
            "/api/v1/groups/permissions",
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 200
        permissions = response.json()
        assert isinstance(permissions, dict)
        # Should have permission categories
        assert "Printers" in permissions or len(permissions) > 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_group(self, async_client: AsyncClient, auth_token: str):
        """Verify creating a new group."""
        response = await async_client.post(
            "/api/v1/groups/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "name": "Custom Group",
                "description": "A custom test group",
                "permissions": ["printers:read", "archives:read"],
            },
        )

        assert response.status_code == 201
        group = response.json()
        assert group["name"] == "Custom Group"
        assert group["description"] == "A custom test group"
        assert "printers:read" in group["permissions"]
        assert group["is_system"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_group(self, async_client: AsyncClient, auth_token: str):
        """Verify updating a group."""
        # Create a group first
        create_response = await async_client.post(
            "/api/v1/groups/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "name": "Update Test Group",
                "permissions": ["printers:read"],
            },
        )
        group_id = create_response.json()["id"]

        # Update the group
        response = await async_client.patch(
            f"/api/v1/groups/{group_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "description": "Updated description",
                "permissions": ["printers:read", "printers:control"],
            },
        )

        assert response.status_code == 200
        group = response.json()
        assert group["description"] == "Updated description"
        assert "printers:control" in group["permissions"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cannot_delete_system_group(self, async_client: AsyncClient, auth_token: str):
        """Verify system groups cannot be deleted."""
        # Get the Administrators group
        list_response = await async_client.get(
            "/api/v1/groups/",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        admin_group = next(g for g in list_response.json() if g["name"] == "Administrators")

        # Try to delete it
        response = await async_client.delete(
            f"/api/v1/groups/{admin_group['id']}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 400
        assert "system group" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_custom_group(self, async_client: AsyncClient, auth_token: str):
        """Verify custom groups can be deleted."""
        # Create a group
        create_response = await async_client.post(
            "/api/v1/groups/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"name": "Delete Test Group"},
        )
        group_id = create_response.json()["id"]

        # Delete it
        response = await async_client.delete(
            f"/api/v1/groups/{group_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 204


class TestUserGroupsAPI:
    """Integration tests for user-group assignments."""

    @pytest.fixture
    async def auth_token(self, async_client: AsyncClient):
        """Return a JWT for the pre-seeded admin."""
        from backend.app.core.auth import create_access_token

        return create_access_token(data={"sub": "test_admin"})

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_user_with_groups(self, async_client: AsyncClient, auth_token: str):
        """Verify creating a user with group assignments."""
        # Get Operators group ID
        groups_response = await async_client.get(
            "/api/v1/groups/",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        operators_group = next(g for g in groups_response.json() if g["name"] == "Operators")

        # Create user with group
        response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "username": "groupuser",
                "password": "password123",
                "group_ids": [operators_group["id"]],
            },
        )

        assert response.status_code == 201
        user = response.json()
        assert any(g["name"] == "Operators" for g in user["groups"])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_user_to_group(self, async_client: AsyncClient, auth_token: str):
        """Verify adding a user to a group."""
        # Create a user
        user_response = await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"username": "addtogroup", "password": "password123"},
        )
        user_id = user_response.json()["id"]

        # Get Viewers group
        groups_response = await async_client.get(
            "/api/v1/groups/",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        viewers_group = next(g for g in groups_response.json() if g["name"] == "Viewers")

        # Add user to group
        response = await async_client.post(
            f"/api/v1/groups/{viewers_group['id']}/users/{user_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        assert response.status_code == 204

        # Verify user is in group
        user_check = await async_client.get(
            f"/api/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert any(g["name"] == "Viewers" for g in user_check.json()["groups"])


class TestChangePasswordAPI:
    """Integration tests for /api/v1/users/me/change-password endpoint."""

    @pytest.fixture
    async def user_token(self, async_client: AsyncClient):
        """Create a regular user and return their JWT."""
        from backend.app.core.auth import create_access_token

        # Use the pre-seeded admin's default auth to create a new regular user.
        await async_client.post(
            "/api/v1/users/",
            json={"username": "pwchangeuser", "password": "oldpassword123"},
        )
        return create_access_token(data={"sub": "pwchangeuser"})

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_change_password_success(self, async_client: AsyncClient, user_token: str):
        """Verify user can change their own password."""
        response = await async_client.post(
            "/api/v1/users/me/change-password",
            headers={"Authorization": f"Bearer {user_token}"},
            json={
                "current_password": "oldpassword123",
                "new_password": "newpassword456",
            },
        )

        assert response.status_code == 200
        assert "success" in response.json()["message"].lower()

        # Verify can login with new password
        login_response = await async_client.post(
            "/api/v1/auth/login",
            json={"username": "pwchangeuser", "password": "newpassword456"},
        )
        assert login_response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_change_password_wrong_current(self, async_client: AsyncClient, user_token: str):
        """Verify changing password fails with wrong current password."""
        response = await async_client.post(
            "/api/v1/users/me/change-password",
            headers={"Authorization": f"Bearer {user_token}"},
            json={
                "current_password": "wrongpassword",
                "new_password": "newpassword456",
            },
        )

        assert response.status_code == 400
        assert "incorrect" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_change_password_requires_auth(self, async_client: AsyncClient):
        """Verify changing password requires authentication."""
        response = await async_client.post(
            "/api/v1/users/me/change-password",
            headers={"Authorization": ""},
            json={
                "current_password": "oldpassword",
                "new_password": "newpassword",
            },
        )

        assert response.status_code == 401


class TestAuthMiddlewarePublicRoutes:
    """Tests for auth middleware public route configuration.

    These routes must be accessible without authentication because browser
    elements like <img src> and <video src> don't send Authorization headers.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_auth_status_is_public(self, async_client: AsyncClient):
        """/api/v1/auth/status is accessible without auth."""
        response = await async_client.get(
            "/api/v1/auth/status",
            headers={"Authorization": ""},
        )
        assert response.status_code == 200
        assert "auth_enabled" in response.json()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_auth_login_is_public(self, async_client: AsyncClient):
        """/api/v1/auth/login is accessible without auth (reaches the login handler)."""
        response = await async_client.post(
            "/api/v1/auth/login",
            headers={"Authorization": ""},
            json={"username": "test_admin", "password": "test_admin_pass"},
        )
        # The middleware lets the request through; the handler returns 200 with a token.
        assert response.status_code == 200
        assert "access_token" in response.json()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_updates_version_is_public(self, async_client: AsyncClient):
        """/api/v1/updates/version is accessible without auth."""
        response = await async_client.get(
            "/api/v1/updates/version",
            headers={"Authorization": ""},
        )
        assert response.status_code != 401

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_protected_route_requires_auth(self, async_client: AsyncClient):
        """Non-public routes return 401 without a token."""
        response = await async_client.get(
            "/api/v1/printers/",
            headers={"Authorization": ""},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_protected_route_works_with_token(self, async_client: AsyncClient):
        """Non-public routes work with the default admin token."""
        # The default async_client header carries the pre-seeded admin JWT.
        response = await async_client.get("/api/v1/printers/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_advanced_auth_status_is_public(self, async_client: AsyncClient):
        """/api/v1/auth/advanced-auth/status is accessible without auth."""
        response = await async_client.get(
            "/api/v1/auth/advanced-auth/status",
            headers={"Authorization": ""},
        )
        assert response.status_code != 401
        if response.status_code == 200:
            result = response.json()
            assert "advanced_auth_enabled" in result
            assert "smtp_configured" in result

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_forgot_password_is_public(self, async_client: AsyncClient):
        """/api/v1/auth/forgot-password is accessible without auth."""
        response = await async_client.post(
            "/api/v1/auth/forgot-password",
            headers={"Authorization": ""},
            json={"email": "test@example.com"},
        )
        assert response.status_code != 401
        # Likely 400 because advanced auth isn't configured - still not 401.
        assert response.status_code in [200, 400]
