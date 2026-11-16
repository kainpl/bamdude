"""The Manyfold connection in backups: the secret stays home, and a partial restore cannot re-aim it."""

import json

import pytest
from sqlalchemy import select

from backend.app.core.credential_config import CONFIG_GUARD_KEY
from backend.app.core.sensitive_settings import is_sensitive_setting
from backend.app.models.settings import Settings
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services.git_backup import GitBackupService
from backend.app.services.git_restore import SETTINGS_PATH, GitRestoreService, _CategoryTally

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

CONNECTION = {
    "manyfold_enabled": "true",
    "manyfold_url": "http://manyfold.lan:3214",
    "manyfold_client_id": "client-abc",
    "manyfold_client_secret": "fabricated-manyfold-secret",
}


async def test_the_client_secret_is_left_out_of_a_git_backup(db_session):
    db_session.add_all([Settings(key=key, value=value) for key, value in CONNECTION.items()])
    await db_session.commit()
    files: dict = {}

    await GitBackupService()._collect_settings(db_session, files)

    exported = files["settings/app_settings.json"]["settings"]
    assert "manyfold_client_secret" not in exported
    assert "fabricated-manyfold-secret" not in json.dumps(files)
    # The rest of the connection is configuration, exported like the other integrations' addresses.
    assert exported["manyfold_url"] == "http://manyfold.lan:3214"
    assert exported["manyfold_client_id"] == "client-abc"


@pytest.mark.parametrize("destination", ["manyfold_url", "manyfold_client_id"])
@pytest.mark.parametrize("has_local_destination", [True, False])
async def test_a_partial_restore_cannot_re_aim_the_kept_secret(db_session, destination, has_local_destination):
    db_session.add(Settings(key="manyfold_client_secret", value="retained-private-credential"))
    if has_local_destination:
        db_session.add(Settings(key=destination, value="original-target"))
    await db_session.commit()
    payload = {
        "settings": {
            destination: "attacker-target",
            "manyfold_client_secret": "imported-secret",
            "currency": "EUR",
            CONFIG_GUARD_KEY: "changed",
        }
    }
    service = GitRestoreService()
    preview, _ = await service._count_items(db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: payload})
    tally = _CategoryTally()
    written: set = set()
    await service._restore_settings(db_session, payload, True, tally, keys_written=written)
    await db_session.commit()

    assert preview == tally.restored == 1
    assert written == {"currency"}
    rows = dict((await db_session.execute(select(Settings.key, Settings.value))).all())
    assert rows["manyfold_client_secret"] == "retained-private-credential"
    assert rows.get(destination) == ("original-target" if has_local_destination else None)


@pytest.mark.parametrize("key", sorted(CONNECTION))
async def test_a_support_bundle_redacts_the_connection(key):
    if key == "manyfold_enabled":
        assert not is_sensitive_setting(key)
    else:
        assert is_sensitive_setting(key)
