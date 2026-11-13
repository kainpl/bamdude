"""E's actual admission/staging on a disposable native PostgreSQL server."""

import pytest

from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.integration.test_g01_e_backup_approval import (
    backup,  # noqa: F401
    test_blocked_commit_failure_and_lost_reply_reconcile_without_duplicate as assert_commit,
    test_occurrences_episode_regrant_and_new_episode as assert_episode,
    test_old_completion_cannot_overwrite_reused_config_and_log_ids as assert_log_identity,
)
from backend.tests.integration.test_g01_e_backup_concurrency import exercise_concurrent_due
from backend.tests.unit.test_g01_e_git_restore_actor import (
    actor_restore,  # noqa: F401
    test_current_actor_success_preserves_original_session_and_canonical_settings as assert_restore_success,
    test_remote_payload_cannot_publish_for_stale_original_actor as assert_restore_revocation,
)
from backend.tests.unit.test_g01_e_git_restore_settings import (
    test_current_schema_ranges_nullable_json_and_canonical_storage as assert_classifier,
)
from backend.tests.unit.test_g01_e_migration import exercise_legacy_migration

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
async def e_anchor(test_engine):
    from backend.app.core.credential_config import initialize_credential_config_anchor

    async with test_engine.begin() as conn:
        await initialize_credential_config_anchor(conn)


async def test_concurrent_due_and_manual_on_postgres(backup):
    await exercise_concurrent_due(backup)


async def test_episode_regrant_and_restart_on_postgres(backup):
    await assert_episode(backup)


@pytest.mark.parametrize("after_commit", [False, True])
async def test_atomic_failure_and_lost_reply_on_postgres(backup, monkeypatch, after_commit):
    await assert_commit(backup, monkeypatch, after_commit)


async def test_attempt_identity_on_postgres(backup):
    await assert_log_identity(backup)


async def test_legacy_approval_migration_on_postgres(test_engine, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))
    await exercise_legacy_migration(test_engine)


async def test_restore_schema_classifier_on_postgres(db_session):
    await assert_classifier(db_session)


async def test_current_restore_actor_on_postgres(actor_restore):
    await assert_restore_success(actor_restore)


async def test_stale_restore_actor_on_postgres(actor_restore, monkeypatch):
    from backend.app.schemas.git_backup import RestoreCategory

    await assert_restore_revocation(actor_restore, "revoke", RestoreCategory.SETTINGS, monkeypatch)
