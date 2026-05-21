"""Tests for the support-bundle feature collectors (upstream Bambuddy #1312).

Each collector reads counts from a dedicated feature table and is
best-effort (one failing collector never blanks the rest of the bundle).
These exercise the real collectors against the in-memory test DB so a
model-field rename can't silently break the bundle.
"""

from __future__ import annotations

import pytest

from backend.app.api.routes.support import (
    _collect_auth_info,
    _collect_git_backup_info,
    _collect_inventory_info,
    _collect_library_info,
    _collect_maintenance_info,
    _collect_queue_info,
)


@pytest.mark.asyncio
async def test_auth_info_shape_on_empty_db(db_session):
    auth = await _collect_auth_info(db_session)
    # Counts default to 0 / empty list, no exceptions on a fresh DB.
    assert auth["oidc_providers"] == []
    assert auth["users_with_totp"] == 0
    assert auth["api_keys_total"] == 0
    assert auth["long_lived_tokens_total"] == 0
    # Groups: system vs custom keys present (default groups may exist
    # depending on fixtures — assert keys exist and are ints).
    assert isinstance(auth["groups_system"], int)
    assert isinstance(auth["groups_custom"], int)


@pytest.mark.asyncio
async def test_library_info_shape_on_empty_db(db_session):
    lib = await _collect_library_info(db_session)
    assert lib["library_files_total"] == 0
    assert lib["library_files_in_trash"] == 0
    assert lib["library_folders_total"] == 0
    assert lib["external_folders_total"] == 0
    assert lib["external_links_total"] == 0
    assert lib["makerworld_imports_total"] == 0


@pytest.mark.asyncio
async def test_inventory_info_shape_on_empty_db(db_session):
    inv = await _collect_inventory_info(db_session)
    assert inv == {
        "spools_internal": 0,
        "k_profiles_internal": 0,
        "k_profiles_spoolman": 0,
    }


@pytest.mark.asyncio
async def test_queue_info_oldest_pending_none_when_empty(db_session):
    q = await _collect_queue_info(db_session)
    assert q["pending_total"] == 0
    assert q["manual_start_pending"] == 0
    assert q["oldest_pending_age_seconds"] is None


@pytest.mark.asyncio
async def test_maintenance_info_shape_on_empty_db(db_session):
    m = await _collect_maintenance_info(db_session)
    assert m == {"items_total": 0, "items_enabled": 0}


@pytest.mark.asyncio
async def test_git_backup_info_shape_on_empty_db(db_session):
    gb = await _collect_git_backup_info(db_session)
    assert gb == {
        "configs_total": 0,
        "providers_used": {},
        "schedule_enabled_count": 0,
        "last_failure_count": 0,
    }


@pytest.mark.asyncio
async def test_library_counts_reflect_rows(db_session):
    """A library file + a makerworld import + a trashed file should each
    land in the right bucket — guards the deleted_at / source_type filters."""
    from datetime import datetime, timezone

    from backend.app.models.library import LibraryFile

    db_session.add_all(
        [
            LibraryFile(filename="a.3mf", file_path="lib/a.3mf", file_type="3mf", file_size=100),
            LibraryFile(
                filename="mw.3mf", file_path="lib/mw.3mf", file_type="3mf", file_size=100, source_type="makerworld"
            ),
            LibraryFile(
                filename="gone.3mf",
                file_path="lib/gone.3mf",
                file_type="3mf",
                file_size=100,
                deleted_at=datetime.now(timezone.utc),
            ),
        ]
    )
    await db_session.commit()

    lib = await _collect_library_info(db_session)
    assert lib["library_files_total"] == 2  # active only (trashed excluded)
    assert lib["library_files_in_trash"] == 1
    assert lib["makerworld_imports_total"] == 1


@pytest.mark.asyncio
async def test_git_backup_provider_histogram(db_session):
    """Provider counts + failure indicator across GitHub / GitLab rows."""
    from backend.app.models.git_backup import GitBackupConfig

    db_session.add_all(
        [
            GitBackupConfig(
                provider="github",
                repository_url="https://github.com/u/repo1",
                access_token="x",
                schedule_enabled=True,
                last_backup_status="success",
            ),
            GitBackupConfig(
                provider="github",
                repository_url="https://github.com/u/repo2",
                access_token="x",
                schedule_enabled=False,
                last_backup_status="failed",
            ),
            GitBackupConfig(
                provider="gitlab",
                repository_url="https://gitlab.com/u/repo3",
                access_token="x",
                schedule_enabled=True,
                last_backup_status=None,
            ),
        ]
    )
    await db_session.commit()

    gb = await _collect_git_backup_info(db_session)
    assert gb["configs_total"] == 3
    assert gb["providers_used"] == {"github": 2, "gitlab": 1}
    assert gb["schedule_enabled_count"] == 2
    assert gb["last_failure_count"] == 1
