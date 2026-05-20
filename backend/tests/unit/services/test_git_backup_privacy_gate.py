"""Unit tests for the Git-backup privacy gate.

Covers two layers of the safety net added to keep BamDude backups from
landing in a public repo:

1. Each provider backend's ``test_connection`` reports ``is_private`` —
   read from ``data.private`` (GitHub / Gitea / Forgejo) or
   ``visibility == "private"`` (GitLab).
2. The exception-path branch on every backend returns ``is_private=None``
   so the gate can fail-closed when visibility can't be determined.

Wiring of the ``_enforce_private_repo`` helper into POST/PATCH ``/config``
and the ``run_backup`` defense-in-depth re-check are covered structurally
here (no live HTTP); the route handlers consume the same shape this
test asserts on.

Ported from upstream Bambuddy commit 48a7024b (#1431 follow-up).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.git_providers.forgejo import ForgejoBackend
from backend.app.services.git_providers.github import GitHubBackend
from backend.app.services.git_providers.gitlab import GitLabBackend


def _ok_response(payload: dict) -> MagicMock:
    """Build an httpx-style response stub for a 200 GET."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=payload)
    return resp


def _client_with(*responses: MagicMock) -> MagicMock:
    """Async client whose .get returns the supplied responses in order."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=list(responses))
    return client


class TestGitHubBackendPrivacy:
    """GitHub + Gitea (inherited) read ``data.private``."""

    @pytest.mark.asyncio
    async def test_private_repo_reports_is_private_true(self):
        backend = GitHubBackend()
        client = _client_with(_ok_response({"full_name": "user/repo", "private": True, "permissions": {"push": True}}))

        result = await backend.test_connection("https://github.com/user/repo", "tok", client)

        assert result["success"] is True
        assert result["is_private"] is True

    @pytest.mark.asyncio
    async def test_public_repo_reports_is_private_false(self):
        backend = GitHubBackend()
        client = _client_with(_ok_response({"full_name": "user/repo", "private": False, "permissions": {"push": True}}))

        result = await backend.test_connection("https://github.com/user/repo", "tok", client)

        assert result["success"] is True
        assert result["is_private"] is False

    @pytest.mark.asyncio
    async def test_missing_private_field_treated_as_non_private(self):
        """If the API never returns ``private`` we treat it as not-confirmed
        and the helper above will reject. Backend itself reports False here
        because the conservative read of ``data.get("private", False)``
        defaults to non-private."""
        backend = GitHubBackend()
        client = _client_with(_ok_response({"full_name": "user/repo", "permissions": {"push": True}}))

        result = await backend.test_connection("https://github.com/user/repo", "tok", client)

        assert result["is_private"] is False

    @pytest.mark.asyncio
    async def test_push_permission_denied_still_carries_visibility(self):
        """A no-push result must still surface visibility so the FE doesn't
        force the user to fix permissions before learning the repo is public."""
        backend = GitHubBackend()
        client = _client_with(
            _ok_response({"full_name": "user/repo", "private": False, "permissions": {"push": False}})
        )

        result = await backend.test_connection("https://github.com/user/repo", "tok", client)

        assert result["success"] is False  # no push
        assert result["is_private"] is False

    @pytest.mark.asyncio
    async def test_exception_path_reports_is_private_none(self):
        backend = GitHubBackend()
        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))

        result = await backend.test_connection("https://github.com/user/repo", "tok", client)

        assert result["success"] is False
        assert result["is_private"] is None


class TestForgejoBackendPrivacy:
    """Forgejo overrides the GitHub path; same ``data.private`` semantics."""

    @pytest.mark.asyncio
    async def test_private_repo(self):
        backend = ForgejoBackend()
        # Forgejo's test_connection does 2 GETs: user + repo. Stub both.
        user_resp = _ok_response({"login": "user"})
        repo_resp = _ok_response({"full_name": "user/repo", "private": True, "permissions": {"push": True}})
        client = _client_with(user_resp, repo_resp)

        result = await backend.test_connection("https://forgejo.example/user/repo", "tok", client)

        assert result["success"] is True
        assert result["is_private"] is True

    @pytest.mark.asyncio
    async def test_public_repo(self):
        backend = ForgejoBackend()
        user_resp = _ok_response({"login": "user"})
        repo_resp = _ok_response({"full_name": "user/repo", "private": False, "permissions": {"push": True}})
        client = _client_with(user_resp, repo_resp)

        result = await backend.test_connection("https://forgejo.example/user/repo", "tok", client)

        assert result["is_private"] is False


class TestGitLabBackendPrivacy:
    """GitLab uses ``visibility`` not ``private``."""

    @pytest.mark.asyncio
    async def test_visibility_private_reports_true(self):
        backend = GitLabBackend()
        client = _client_with(
            _ok_response(
                {
                    "name_with_namespace": "Group / Project",
                    "visibility": "private",
                    "permissions": {"project_access": {"access_level": 40}},
                }
            )
        )

        result = await backend.test_connection("https://gitlab.com/group/project", "tok", client)

        assert result["success"] is True
        assert result["is_private"] is True

    @pytest.mark.asyncio
    async def test_visibility_internal_is_not_private(self):
        """GitLab's 'internal' visibility = signed-in users → NOT private for
        our credentials-bundle purposes."""
        backend = GitLabBackend()
        client = _client_with(
            _ok_response(
                {
                    "name_with_namespace": "Group / Project",
                    "visibility": "internal",
                    "permissions": {"project_access": {"access_level": 40}},
                }
            )
        )

        result = await backend.test_connection("https://gitlab.com/group/project", "tok", client)

        assert result["is_private"] is False

    @pytest.mark.asyncio
    async def test_visibility_public_reports_false(self):
        backend = GitLabBackend()
        client = _client_with(
            _ok_response(
                {
                    "name_with_namespace": "Group / Project",
                    "visibility": "public",
                    "permissions": {"project_access": {"access_level": 40}},
                }
            )
        )

        result = await backend.test_connection("https://gitlab.com/group/project", "tok", client)

        assert result["is_private"] is False

    @pytest.mark.asyncio
    async def test_missing_visibility_field_is_not_private(self):
        """If GitLab ever drops the field, fail closed (None at the helper layer)."""
        backend = GitLabBackend()
        client = _client_with(
            _ok_response(
                {
                    "name_with_namespace": "Group / Project",
                    "permissions": {"project_access": {"access_level": 40}},
                }
            )
        )

        result = await backend.test_connection("https://gitlab.com/group/project", "tok", client)

        assert result["is_private"] is False

    @pytest.mark.asyncio
    async def test_exception_path_reports_is_private_none(self):
        backend = GitLabBackend()
        client = MagicMock()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))

        result = await backend.test_connection("https://gitlab.com/group/project", "tok", client)

        assert result["success"] is False
        assert result["is_private"] is None


class TestEnforcePrivateRepoHelper:
    """Route-layer helper — rejects on non-private + unknown-visibility."""

    @pytest.mark.asyncio
    async def test_rejects_public_repo(self):
        from fastapi import HTTPException

        from backend.app.api.routes.git_backup import _enforce_private_repo

        async def _stub(repo_url, token, provider=None):  # noqa: ARG001
            return {"success": True, "is_private": False, "message": "Connection successful"}

        from backend.app.services import git_backup as svc

        original = svc.git_backup_service.test_connection
        svc.git_backup_service.test_connection = _stub
        try:
            with pytest.raises(HTTPException) as exc_info:
                await _enforce_private_repo("https://github.com/u/r", "tok", "github")
            assert exc_info.value.status_code == 400
            assert "not private" in exc_info.value.detail.lower()
        finally:
            svc.git_backup_service.test_connection = original

    @pytest.mark.asyncio
    async def test_rejects_unknown_visibility(self):
        from fastapi import HTTPException

        from backend.app.api.routes.git_backup import _enforce_private_repo

        async def _stub(repo_url, token, provider=None):  # noqa: ARG001
            return {"success": True, "is_private": None, "message": "Connection successful"}

        from backend.app.services import git_backup as svc

        original = svc.git_backup_service.test_connection
        svc.git_backup_service.test_connection = _stub
        try:
            with pytest.raises(HTTPException) as exc_info:
                await _enforce_private_repo("https://github.com/u/r", "tok", "github")
            assert exc_info.value.status_code == 400
            assert "could not confirm" in exc_info.value.detail.lower()
        finally:
            svc.git_backup_service.test_connection = original

    @pytest.mark.asyncio
    async def test_accepts_confirmed_private(self):
        from backend.app.api.routes.git_backup import _enforce_private_repo

        async def _stub(repo_url, token, provider=None):  # noqa: ARG001
            return {"success": True, "is_private": True, "message": "Connection successful"}

        from backend.app.services import git_backup as svc

        original = svc.git_backup_service.test_connection
        svc.git_backup_service.test_connection = _stub
        try:
            # Should NOT raise.
            await _enforce_private_repo("https://github.com/u/r", "tok", "github")
        finally:
            svc.git_backup_service.test_connection = original

    @pytest.mark.asyncio
    async def test_rejects_failed_connection_test(self):
        from fastapi import HTTPException

        from backend.app.api.routes.git_backup import _enforce_private_repo

        async def _stub(repo_url, token, provider=None):  # noqa: ARG001
            return {"success": False, "is_private": None, "message": "Invalid access token"}

        from backend.app.services import git_backup as svc

        original = svc.git_backup_service.test_connection
        svc.git_backup_service.test_connection = _stub
        try:
            with pytest.raises(HTTPException) as exc_info:
                await _enforce_private_repo("https://github.com/u/r", "tok", "github")
            assert exc_info.value.status_code == 400
            assert "cannot verify repository" in exc_info.value.detail.lower()
        finally:
            svc.git_backup_service.test_connection = original
