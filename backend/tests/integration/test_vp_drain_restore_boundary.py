"""Typed409 before Git categories and catalog translation through real HTTP."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.i18n import set_language_cache
from backend.app.models.git_backup import GitBackupLog
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services.farm_startup_guard import farm_startup_guard
from backend.app.services.git_restore import GitRestoreService
from backend.app.services.virtual_printer import virtual_printer_manager
from backend.app.services.virtual_printer.lifecycle import VP_DRAIN_MESSAGE, VPDrainRefused
from backend.tests.integration.test_git_restore_api import _create_config


@pytest.fixture
async def config(async_client):
    with patch(
        "backend.app.services.git_backup.git_backup_service.test_connection",
        AsyncMock(
            return_value={
                "success": True,
                "message": "OK",
                "is_private": True,
                "repo_name": "test/repo",
                "permissions": {"push": True},
            }
        ),
    ):
        return await _create_config(async_client)


@pytest.mark.parametrize("language", ["en", "uk"])
async def test_http409_is_translated_without_success_shape(async_client, config, language):
    set_language_cache(language)
    try:
        with patch(
            "backend.app.services.git_restore.git_restore_service.run_restore", AsyncMock(side_effect=VPDrainRefused())
        ):
            response = await async_client.post(
                "/api/v1/git-backup/restore",
                json={
                    "ref": "a" * 40,
                    "categories": ["archives"],
                    "overwrite_existing": False,
                },
            )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail == VP_DRAIN_MESSAGE if language == "en" else "повторіть у цьому процесі" in detail
        assert "success" not in response.json()
    finally:
        set_language_cache("en")


async def test_actual_service_refuses_before_apply_and_finishes_its_log(test_engine, config, monkeypatch):
    from backend.app.services import git_restore

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(git_restore, "async_session", sessions)
    service = GitRestoreService()
    monkeypatch.setattr(service, "_resolve_ref", AsyncMock(return_value=("a" * 40, None, None)))
    monkeypatch.setattr(service, "_read_categories", AsyncMock(return_value=({}, None)))
    apply = AsyncMock()
    monkeypatch.setattr(service, "_apply", apply)
    monkeypatch.setattr(virtual_printer_manager, "drain_for_restore", AsyncMock(side_effect=VPDrainRefused()))
    monkeypatch.setattr(farm_startup_guard, "restart_required", False)
    monkeypatch.setattr(farm_startup_guard, "active", False)
    with pytest.raises(VPDrainRefused):
        await service.run_restore(config["id"], "a" * 40, [RestoreCategory.ARCHIVES])
    apply.assert_not_awaited()
    assert farm_startup_guard.restart_required
    assert not service.is_running
    async with sessions() as db:
        row = (await db.execute(select(GitBackupLog))).scalar_one()
        assert row.status == "failed" and row.files_changed == 0
