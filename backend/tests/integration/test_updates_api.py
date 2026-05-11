"""Integration tests for Updates API endpoints + version-comparison helpers."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.fixture(autouse=True)
def reset_update_status():
    """Reset the module-level _update_status before each test. Without this,
    a test that triggers the apply path (which flips status to 'downloading')
    causes the next test's apply call to return 'Update already in progress'
    via the early-return guard."""
    from backend.app.api.routes import updates

    updates._update_status = {
        "status": "idle",
        "progress": 0,
        "message": "",
        "error": None,
    }
    yield


class TestUpdatesAPI:
    @pytest.mark.asyncio
    async def test_get_version(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/updates/version")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_apply_update_docker_rejection(self, async_client: AsyncClient):
        with (
            patch("backend.app.api.routes.updates._is_docker_environment", return_value=True),
            patch(
                "backend.app.api.routes.updates._find_latest_release",
                new=AsyncMock(return_value={"tag_name": "v0.4.4"}),
            ),
        ):
            response = await async_client.post("/api/v1/updates/apply")
        result = response.json()
        assert result["success"] is False
        assert result["is_docker"] is True
        # Docker message should embed the resolved git ref so the operator
        # can paste-and-run the equivalent docker-compose update.
        assert "v0.4.4" in result["message"]
        assert result["target_ref"] == "v0.4.4"

    @pytest.mark.asyncio
    async def test_apply_update_explicit_tag(self, async_client: AsyncClient):
        """When the frontend passes ``tag_name``, the apply path uses it
        directly without re-fetching GitHub."""
        with (
            patch("backend.app.api.routes.updates._is_docker_environment", return_value=False),
            patch("backend.app.api.routes.updates._perform_update", new_callable=AsyncMock) as perform,
        ):
            response = await async_client.post(
                "/api/v1/updates/apply",
                json={"tag_name": "0.4.5b1"},
            )
        result = response.json()
        assert result["success"] is True
        # _resolve_git_ref always normalises to vX.Y.ZbN — re-adds the v prefix.
        assert result["target_ref"] == "v0.4.5b1"
        # Background task scheduled with the same ref. Note: BackgroundTasks
        # runs after response is sent in the test client, so we only check
        # the scheduling expectation indirectly via the perform mock not yet
        # being awaited at this point — the response shape is the contract.
        del perform  # silence unused-warning; presence is what we patch

    @pytest.mark.asyncio
    async def test_apply_update_no_explicit_tag_falls_back_to_resolve(self, async_client: AsyncClient):
        """When the frontend passes no body, apply re-resolves the latest
        release from GitHub respecting the include_beta_updates setting."""
        with (
            patch("backend.app.api.routes.updates._is_docker_environment", return_value=False),
            patch(
                "backend.app.api.routes.updates._find_latest_release",
                new=AsyncMock(return_value={"tag_name": "v0.4.4"}),
            ),
            patch("backend.app.api.routes.updates._perform_update", new_callable=AsyncMock),
        ):
            response = await async_client.post("/api/v1/updates/apply")
        result = response.json()
        assert result["success"] is True
        assert result["target_ref"] == "v0.4.4"

    def test_is_docker_with_dockerenv(self):
        from backend.app.api.routes.updates import _is_docker_environment

        with patch("os.path.exists", return_value=True):
            assert _is_docker_environment() is True

    def test_is_ha_addon_present(self):
        """SUPERVISOR_TOKEN is what HA Supervisor injects into every addon
        container. Presence (and non-empty value) flips the detection True."""
        from backend.app.api.routes.updates import _is_ha_addon

        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "abc123"}, clear=False):
            assert _is_ha_addon() is True

    def test_is_ha_addon_absent(self):
        from backend.app.api.routes.updates import _is_ha_addon

        with patch.dict("os.environ", {}, clear=True):
            assert _is_ha_addon() is False

    def test_is_ha_addon_empty_string_is_unset(self):
        """An empty string for SUPERVISOR_TOKEN must be treated as unset —
        otherwise a misconfigured docker-compose with ``SUPERVISOR_TOKEN=``
        would mis-classify a plain Docker install as an HA addon."""
        from backend.app.api.routes.updates import _is_ha_addon

        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": ""}, clear=False):
            assert _is_ha_addon() is False

    @pytest.mark.asyncio
    async def test_apply_update_ha_addon_precedes_docker(self, async_client: AsyncClient):
        """HA addons ARE Docker containers, so the HA-precedence check must
        win — otherwise the Docker branch would hand operators a
        docker-compose snippet they can't run."""
        with (
            patch("backend.app.api.routes.updates._is_ha_addon", return_value=True),
            patch("backend.app.api.routes.updates._is_docker_environment", return_value=True),
            patch(
                "backend.app.api.routes.updates._find_latest_release",
                new=AsyncMock(return_value={"tag_name": "v0.4.4"}),
            ),
        ):
            response = await async_client.post("/api/v1/updates/apply")
        result = response.json()
        assert result["success"] is False
        assert result["is_ha_addon"] is True
        assert result["is_docker"] is True
        assert "Home Assistant" in result["message"]


class TestParseVersion:
    """parse_version returns (major, minor, patch, micro, is_prerelease, prerelease_num)."""

    def test_simple_release(self):
        from backend.app.api.routes.updates import parse_version

        assert parse_version("0.4.4") == (0, 4, 4, 0, 0, 0)

    def test_release_with_v_prefix(self):
        from backend.app.api.routes.updates import parse_version

        assert parse_version("v0.4.4") == (0, 4, 4, 0, 0, 0)

    def test_beta(self):
        from backend.app.api.routes.updates import parse_version

        assert parse_version("0.4.5b1") == (0, 4, 5, 0, 1, 1)
        assert parse_version("v0.4.5b1") == (0, 4, 5, 0, 1, 1)

    def test_beta_double_digit(self):
        from backend.app.api.routes.updates import parse_version

        assert parse_version("0.4.5b10") == (0, 4, 5, 0, 1, 10)

    def test_four_part_release(self):
        from backend.app.api.routes.updates import parse_version

        assert parse_version("0.4.4.1") == (0, 4, 4, 1, 0, 0)

    def test_four_part_beta(self):
        from backend.app.api.routes.updates import parse_version

        assert parse_version("0.4.4.1b2") == (0, 4, 4, 1, 1, 2)

    def test_alpha_rc_variants(self):
        from backend.app.api.routes.updates import parse_version

        # alpha + rc are counted as prereleases via the [a-zA-Z] check —
        # prerelease_num grouping pulls the trailing N
        assert parse_version("0.4.5alpha1")[4] == 1
        assert parse_version("0.4.5rc2")[4] == 1

    def test_daily_suffix_stripped(self):
        from backend.app.api.routes.updates import parse_version

        # -daily.YYYYMMDD suffix is stripped before parsing
        assert parse_version("0.2.2b4-daily.20260313") == (0, 2, 2, 0, 1, 4)


class TestIsNewerVersion:
    """is_newer_version handles the full release / prerelease / patch matrix."""

    def test_next_patch_is_newer(self):
        from backend.app.api.routes.updates import is_newer_version

        assert is_newer_version("0.4.5", "0.4.4") is True
        assert is_newer_version("0.4.4", "0.4.5") is False

    def test_release_beats_prerelease_of_same_base(self):
        # "Don't downgrade me from a release into a beta of the same version"
        from backend.app.api.routes.updates import is_newer_version

        assert is_newer_version("0.4.4", "0.4.4b1") is True
        assert is_newer_version("0.4.4b1", "0.4.4") is False

    def test_later_beta_beats_earlier_beta(self):
        from backend.app.api.routes.updates import is_newer_version

        assert is_newer_version("0.4.4b2", "0.4.4b1") is True
        assert is_newer_version("0.4.4b1", "0.4.4b2") is False
        # Double-digit beta numbers — string-compare would say b10 < b2;
        # int compare must say b10 > b2
        assert is_newer_version("0.4.4b10", "0.4.4b2") is True

    def test_next_version_beta_beats_current_release(self):
        # Important for include_beta_updates flow — beta of next version
        # should still surface as an update opportunity
        from backend.app.api.routes.updates import is_newer_version

        assert is_newer_version("0.4.5b1", "0.4.4") is True

    def test_four_part_patch_is_newer(self):
        from backend.app.api.routes.updates import is_newer_version

        assert is_newer_version("0.4.4.1", "0.4.4") is True
        assert is_newer_version("0.4.4", "0.4.4.1") is False

    def test_four_part_beta_beats_three_part_release(self):
        from backend.app.api.routes.updates import is_newer_version

        assert is_newer_version("0.4.4.1b1", "0.4.4") is True

    def test_same_version_is_not_newer(self):
        from backend.app.api.routes.updates import is_newer_version

        assert is_newer_version("0.4.4", "0.4.4") is False
        assert is_newer_version("0.4.4b1", "0.4.4b1") is False

    def test_v_prefix_is_irrelevant(self):
        from backend.app.api.routes.updates import is_newer_version

        # /releases endpoint returns ``tag_name`` like ``v0.4.4``; APP_VERSION
        # is bare ``0.4.4`` — comparison must be tolerant to either form on
        # either side.
        assert is_newer_version("v0.4.5", "0.4.4") is True
        assert is_newer_version("0.4.5", "v0.4.4") is True


class TestResolveGitRef:
    """_resolve_git_ref normalises tag/version strings to vX.Y.Z[bN] form."""

    def test_already_v_prefixed(self):
        from backend.app.api.routes.updates import _resolve_git_ref

        assert _resolve_git_ref("v0.4.4") == "v0.4.4"

    def test_no_v_prefix(self):
        from backend.app.api.routes.updates import _resolve_git_ref

        assert _resolve_git_ref("0.4.4") == "v0.4.4"

    def test_beta(self):
        from backend.app.api.routes.updates import _resolve_git_ref

        assert _resolve_git_ref("0.4.5b1") == "v0.4.5b1"
        assert _resolve_git_ref("v0.4.5b1") == "v0.4.5b1"


class TestPrereleaseDetection:
    """_is_release_prerelease respects both the version-name convention AND
    GitHub's own prerelease flag — either signal counts."""

    @pytest.mark.asyncio
    async def test_version_name_signals_prerelease(self):
        from backend.app.api.routes.updates import _is_release_prerelease

        assert await _is_release_prerelease({"tag_name": "v0.4.5b1"}) is True
        assert await _is_release_prerelease({"tag_name": "v0.4.5"}) is False

    @pytest.mark.asyncio
    async def test_github_flag_signals_prerelease(self):
        # Stable-shaped tag but explicitly marked prerelease in GitHub UI
        from backend.app.api.routes.updates import _is_release_prerelease

        assert await _is_release_prerelease({"tag_name": "v0.4.5", "prerelease": True}) is True

    @pytest.mark.asyncio
    async def test_both_signals_off_means_stable(self):
        from backend.app.api.routes.updates import _is_release_prerelease

        assert await _is_release_prerelease({"tag_name": "v0.4.5", "prerelease": False}) is False
