"""Integration tests for Git Backup API endpoints."""

import pytest
from httpx import AsyncClient


@pytest.fixture(autouse=True)
def _stub_privacy_gate(monkeypatch):
    """Bypass the live privacy-gate test_connection in config-mutation tests.

    POST/PATCH ``/git-backup/config`` now runs an internal test_connection
    against the supplied URL/token and refuses non-private repos. These
    integration tests use synthetic URLs/tokens which would always 404, so
    the gate would reject every create/update with 400 ("Cannot verify
    repository"). Stub the service-level test_connection to return a
    private-repo success so the existing config-shape assertions continue
    to cover what they were built to cover. Privacy-gate behaviour itself
    is covered by ``test_git_backup_privacy_gate.py``.
    """
    from backend.app.services import git_backup as svc

    async def _stub(repo_url, token, provider="github", api_base_url=None):  # noqa: ARG001
        return {
            "success": True,
            "message": "Connection successful",
            "repo_name": "test/repo",
            "permissions": {"push": True},
            "is_private": True,
        }

    monkeypatch.setattr(svc.git_backup_service, "test_connection", _stub)


class TestGitBackupConfigAPI:
    """Integration tests for /api/v1/git-backup endpoints."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_config_no_config(self, async_client: AsyncClient):
        """Verify getting config when none exists returns null."""
        response = await async_client.get("/api/v1/git-backup/config")
        assert response.status_code == 200
        assert response.json() is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_config(self, async_client: AsyncClient):
        """Verify Git backup config can be created."""
        data = {
            "provider": "github",
            "repository_url": "https://github.com/test/repo",
            "access_token": "ghp_testtoken123",
            "branch": "main",
            "schedule_enabled": False,
            "schedule_type": "daily",
            "backup_kprofiles": True,
            "backup_cloud_profiles": True,
            "backup_settings": False,
            "enabled": True,
        }
        response = await async_client.post("/api/v1/git-backup/config", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["repository_url"] == "https://github.com/test/repo"
        assert result["branch"] == "main"
        assert result["has_token"] is True
        assert result["enabled"] is True
        assert result["provider"] == "github"
        # Token should not be exposed in response
        assert "access_token" not in result

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_config_after_create(self, async_client: AsyncClient):
        """Verify getting config after creation returns the config."""
        # Create config first
        data = {
            "provider": "github",
            "repository_url": "https://github.com/test/getrepo",
            "access_token": "ghp_testtoken456",
            "branch": "develop",
            "schedule_enabled": True,
            "schedule_type": "weekly",
            "backup_kprofiles": True,
            "backup_cloud_profiles": False,
            "backup_settings": True,
            "enabled": True,
        }
        await async_client.post("/api/v1/git-backup/config", json=data)

        # Get config
        response = await async_client.get("/api/v1/git-backup/config")
        assert response.status_code == 200
        result = response.json()
        assert result is not None
        assert result["repository_url"] == "https://github.com/test/getrepo"
        assert result["branch"] == "develop"
        assert result["schedule_type"] == "weekly"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_config_partial(self, async_client: AsyncClient):
        """Verify partial update of Git backup config."""
        # Create config first
        create_data = {
            "provider": "github",
            "repository_url": "https://github.com/test/update",
            "access_token": "ghp_token",
            "branch": "main",
            "schedule_enabled": False,
            "schedule_type": "daily",
            "backup_kprofiles": True,
            "backup_cloud_profiles": True,
            "backup_settings": False,
            "enabled": True,
        }
        await async_client.post("/api/v1/git-backup/config", json=create_data)

        # Partial update
        update_data = {
            "branch": "develop",
            "schedule_enabled": True,
        }
        response = await async_client.patch("/api/v1/git-backup/config", json=update_data)
        assert response.status_code == 200
        result = response.json()
        assert result["branch"] == "develop"
        assert result["schedule_enabled"] is True
        # Original values should be preserved
        assert result["repository_url"] == "https://github.com/test/update"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_config(self, async_client: AsyncClient):
        """Verify Git backup config can be deleted."""
        # Create config first
        create_data = {
            "provider": "github",
            "repository_url": "https://github.com/test/delete",
            "access_token": "ghp_deletetoken",
            "branch": "main",
            "schedule_enabled": False,
            "schedule_type": "daily",
            "backup_kprofiles": True,
            "backup_cloud_profiles": True,
            "backup_settings": False,
            "enabled": True,
        }
        await async_client.post("/api/v1/git-backup/config", json=create_data)

        # Delete
        response = await async_client.delete("/api/v1/git-backup/config")
        assert response.status_code == 200

        # Verify it's deleted
        get_response = await async_client.get("/api/v1/git-backup/config")
        assert get_response.status_code == 200
        assert get_response.json() is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_config_not_found(self, async_client: AsyncClient):
        """Verify deleting non-existent config returns 404."""
        # Make sure no config exists
        await async_client.delete("/api/v1/git-backup/config")

        # Try to delete again
        response = await async_client.delete("/api/v1/git-backup/config")
        assert response.status_code == 404


class TestGitBackupStatusAPI:
    """Integration tests for /api/v1/git-backup/status endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_no_config(self, async_client: AsyncClient):
        """Verify status when no config exists."""
        # Ensure no config
        await async_client.delete("/api/v1/git-backup/config")

        response = await async_client.get("/api/v1/git-backup/status")
        assert response.status_code == 200
        result = response.json()
        assert result["configured"] is False
        assert result["enabled"] is False
        assert result["is_running"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_with_config(self, async_client: AsyncClient):
        """Verify status when config exists."""
        # Create config
        create_data = {
            "provider": "github",
            "repository_url": "https://github.com/test/status",
            "access_token": "ghp_statustoken",
            "branch": "main",
            "schedule_enabled": True,
            "schedule_type": "hourly",
            "backup_kprofiles": True,
            "backup_cloud_profiles": True,
            "backup_settings": False,
            "enabled": True,
        }
        await async_client.post("/api/v1/git-backup/config", json=create_data)

        response = await async_client.get("/api/v1/git-backup/status")
        assert response.status_code == 200
        result = response.json()
        assert result["configured"] is True
        assert result["enabled"] is True
        assert result["is_running"] is False
        assert result["next_scheduled_run"] is not None


class TestGitBackupLogsAPI:
    """Integration tests for /api/v1/git-backup/logs endpoints."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_logs_no_config(self, async_client: AsyncClient):
        """Verify getting logs when no config exists returns empty list."""
        # Ensure no config
        await async_client.delete("/api/v1/git-backup/config")

        response = await async_client.get("/api/v1/git-backup/logs")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_logs_with_config(self, async_client: AsyncClient):
        """Verify getting logs with config."""
        # Create config
        create_data = {
            "provider": "github",
            "repository_url": "https://github.com/test/logs",
            "access_token": "ghp_logstoken",
            "branch": "main",
            "schedule_enabled": False,
            "schedule_type": "daily",
            "backup_kprofiles": True,
            "backup_cloud_profiles": True,
            "backup_settings": False,
            "enabled": True,
        }
        await async_client.post("/api/v1/git-backup/config", json=create_data)

        response = await async_client.get("/api/v1/git-backup/logs")
        assert response.status_code == 200
        # No backups run yet, so empty list
        assert response.json() == []


class TestGitBackupTriggerAPI:
    """Integration tests for /api/v1/git-backup/run endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_trigger_no_config(self, async_client: AsyncClient):
        """Verify triggering backup without config returns 404."""
        # Ensure no config
        await async_client.delete("/api/v1/git-backup/config")

        response = await async_client.post("/api/v1/git-backup/run")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_trigger_disabled_config(self, async_client: AsyncClient):
        """Verify triggering backup with disabled config returns 400."""
        # Create disabled config
        create_data = {
            "provider": "github",
            "repository_url": "https://github.com/test/trigger",
            "access_token": "ghp_triggertoken",
            "branch": "main",
            "schedule_enabled": False,
            "schedule_type": "daily",
            "backup_kprofiles": True,
            "backup_cloud_profiles": True,
            "backup_settings": False,
            "enabled": False,  # Disabled
        }
        await async_client.post("/api/v1/git-backup/config", json=create_data)

        response = await async_client.post("/api/v1/git-backup/run")
        assert response.status_code == 400
        assert "disabled" in response.json()["detail"].lower()
