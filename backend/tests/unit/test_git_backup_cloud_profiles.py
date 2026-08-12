"""Git backup collects cloud profiles from every connected account (upstream #2717).

Enabling Cloud Profiles for a Git backup produced nothing and said it had worked.
Two independent faults, either one sufficient:

**Where the credentials live.** The collector read ``bambu_cloud_token`` from the
Settings table — the *auth-disabled* store. BamDude's auth is always on by
invariant, so tokens live on ``User`` rows and that key is never populated on a
normal install. The collector returned at "not authenticated" every time, which
is why the second fault was never even reached.

**The response shape.** Bambu's listing endpoint is keyed by preset type, each
key holding ``private``/``public`` arrays; entries carry no ``type`` of their
own. The collector iterated ``data["setting"]`` and read ``entry["type"]``, so
the loop body never executed once — and would have classified nothing if it had.
``routes/cloud.py`` already had this right, including that the API calls process
presets "print".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.git_backup import _BAMBU_PRESET_TYPES, GitBackupService, _slugify_account

# The real shape, as routes/cloud.py reads it.
LISTING = {
    "filament": {"private": [{"setting_id": "P1", "name": "My PLA"}], "public": []},
    "printer": {"private": [], "public": [{"setting_id": "P2", "name": "X1C"}]},
    "print": {"private": [{"setting_id": "P3", "name": "0.20 Standard"}], "public": []},
}


def _db_returning(users: list, settings: list):
    """A db whose successive execute() calls yield users then settings rows."""
    db = MagicMock()
    results = []
    for rows in (users, settings):
        r = MagicMock()
        r.scalars.return_value.all.return_value = rows
        results.append(r)
    db.execute = AsyncMock(side_effect=results)
    return db


def _user(uid: int, token: str, email: str, region: str = "global"):
    return SimpleNamespace(id=uid, cloud_token=token, cloud_email=email, cloud_region=region)


def _cloud(listing=LISTING, authenticated: bool = True):
    cloud = MagicMock()
    cloud.is_authenticated = authenticated
    cloud.get_slicer_settings = AsyncMock(return_value=listing)
    cloud.close = AsyncMock()
    cloud.set_token = MagicMock()
    return cloud


class TestTheResponseShape:
    @pytest.mark.asyncio
    async def test_presets_are_read_from_the_type_keyed_listing(self) -> None:
        files: dict = {}
        db = _db_returning([_user(1, "tok", "joe@example.com")], [])

        with patch("backend.app.services.git_backup.BambuCloudService", return_value=_cloud()):
            await GitBackupService()._collect_cloud_profiles(db, files)

        assert set(files) == {
            "cloud_profiles/filament.json",
            "cloud_profiles/printer.json",
            "cloud_profiles/process.json",
        }
        assert files["cloud_profiles/filament.json"]["profiles"][0]["name"] == "My PLA"

    @pytest.mark.asyncio
    async def test_the_api_calls_process_presets_print(self) -> None:
        """The one rename in the mapping, and the one most likely to be dropped
        by a future edit that "tidies" the constant."""
        assert _BAMBU_PRESET_TYPES["print"] == "process"

        files: dict = {}
        db = _db_returning([_user(1, "tok", "joe@example.com")], [])
        with patch("backend.app.services.git_backup.BambuCloudService", return_value=_cloud()):
            await GitBackupService()._collect_cloud_profiles(db, files)

        assert files["cloud_profiles/process.json"]["profiles"][0]["name"] == "0.20 Standard"

    @pytest.mark.asyncio
    async def test_private_and_public_presets_are_both_kept(self) -> None:
        files: dict = {}
        db = _db_returning([_user(1, "tok", "joe@example.com")], [])
        listing = {"filament": {"private": [{"setting_id": "a"}], "public": [{"setting_id": "b"}]}}

        with patch("backend.app.services.git_backup.BambuCloudService", return_value=_cloud(listing)):
            await GitBackupService()._collect_cloud_profiles(db, files)

        assert len(files["cloud_profiles/filament.json"]["profiles"]) == 2

    @pytest.mark.asyncio
    async def test_the_old_shape_produced_nothing(self) -> None:
        """States the bug: a response with no ``setting`` key is exactly what the
        old loop was iterating, and it yielded zero files while reporting success.
        """
        assert "setting" not in LISTING


class TestWhereTheCredentialsLive:
    @pytest.mark.asyncio
    async def test_a_user_token_is_used(self) -> None:
        """The case that was broken on every normal install: auth is always on,
        so the token is on the User row and never in Settings."""
        files: dict = {}
        db = _db_returning([_user(1, "user-token", "joe@example.com", "china")], [])
        cloud = _cloud()

        with patch("backend.app.services.git_backup.BambuCloudService", return_value=cloud) as ctor:
            await GitBackupService()._collect_cloud_profiles(db, files)

        cloud.set_token.assert_called_once_with("user-token")
        assert ctor.call_args.kwargs["region"] == "china"
        assert files

    @pytest.mark.asyncio
    async def test_every_connected_account_is_collected(self) -> None:
        """Backing up only the first would silently drop the rest on a
        multi-admin install."""
        files: dict = {}
        db = _db_returning([_user(1, "t1", "a@example.com"), _user(2, "t2", "b@example.com")], [])

        with patch("backend.app.services.git_backup.BambuCloudService", return_value=_cloud()):
            await GitBackupService()._collect_cloud_profiles(db, files)

        assert "cloud_profiles/a-example.com/filament.json" in files
        assert "cloud_profiles/b-example.com/filament.json" in files

    @pytest.mark.asyncio
    async def test_a_single_account_keeps_the_flat_paths(self) -> None:
        """So an existing backup repository does not sprout a parallel tree on
        upgrade."""
        files: dict = {}
        db = _db_returning([_user(1, "tok", "joe@example.com")], [])

        with patch("backend.app.services.git_backup.BambuCloudService", return_value=_cloud()):
            await GitBackupService()._collect_cloud_profiles(db, files)

        assert "cloud_profiles/filament.json" in files

    @pytest.mark.asyncio
    async def test_the_settings_fallback_still_works_when_no_user_has_a_token(self) -> None:
        """Ownerless API-key installs — the case the Settings store was for."""
        files: dict = {}
        db = _db_returning([], [SimpleNamespace(key="bambu_cloud_token", value="global-token")])
        cloud = _cloud()

        with patch("backend.app.services.git_backup.BambuCloudService", return_value=cloud):
            await GitBackupService()._collect_cloud_profiles(db, files)

        cloud.set_token.assert_called_once_with("global-token")
        assert "cloud_profiles/filament.json" in files

    @pytest.mark.asyncio
    async def test_no_credentials_anywhere_collects_nothing(self) -> None:
        files: dict = {}
        db = _db_returning([], [])

        with patch("backend.app.services.git_backup.BambuCloudService") as ctor:
            await GitBackupService()._collect_cloud_profiles(db, files)

        ctor.assert_not_called()
        assert files == {}


class TestSlugify:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [("joe@example.com", "joe-example.com"), ("", "account"), ("--", "account"), ("A b/c", "A-b-c")],
    )
    def test_account_folder_names_are_path_safe(self, label: str, expected: str) -> None:
        assert _slugify_account(label) == expected
