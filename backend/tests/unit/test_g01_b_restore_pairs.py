"""B partial restore cannot become a parallel destination editor."""

import pytest
from sqlalchemy import select

from backend.app.core.credential_config import CONFIG_GUARD_KEY
from backend.app.models.settings import Settings
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services.git_restore import SETTINGS_PATH, GitRestoreService, _CategoryTally


@pytest.mark.parametrize(
    "destination,credential",
    [
        ("ha_url", "ha_token"),
        ("mqtt_broker", "mqtt_password"),
        ("mqtt_username", "mqtt_password"),
        ("mqtt_use_tls", "mqtt_password"),
        ("obico_ml_url", "obico_ml_token"),
        ("smtp_host", "smtp_password"),
        ("smtp_security", "smtp_password"),
        ("smtp_auth_enabled", "smtp_password"),
        ("ldap_server_url", "ldap_bind_password"),
        ("ldap_ca_cert_path", "ldap_bind_password"),
    ],
)
@pytest.mark.parametrize("has_local_destination", [True, False])
async def test_preview_and_apply_refuse_rebinding_a_local_credential(
    db_session, destination, credential, has_local_destination
):
    db_session.add(Settings(key=credential, value="retained-private-credential"))
    if has_local_destination:
        db_session.add(Settings(key=destination, value="original-target"))
    await db_session.commit()
    payload = {
        "settings": {
            destination: "attacker-target",
            credential: "imported-secret",
            "currency": "EUR",
            CONFIG_GUARD_KEY: "changed",
        }
    }
    service = GitRestoreService()
    preview, _ = await service._count_items(db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: payload})
    tally = _CategoryTally()
    written = set()
    await service._restore_settings(db_session, payload, True, tally, keys_written=written)
    await db_session.commit()
    assert preview == tally.restored == 1
    assert written == {"currency"}
    rows = dict((await db_session.execute(select(Settings.key, Settings.value))).all())
    assert rows[credential] == "retained-private-credential"
    assert rows.get(destination) == ("original-target" if has_local_destination else None)
    assert rows[CONFIG_GUARD_KEY] == "1"


async def test_env_only_ha_token_cannot_be_rebound_by_partial_restore(db_session, monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "environment-only-credential")
    service = GitRestoreService()
    tally = _CategoryTally()
    await service._restore_settings(db_session, {"settings": {"ha_url": "https://attacker.invalid"}}, True, tally)
    await db_session.commit()
    assert await db_session.scalar(select(Settings.value).where(Settings.key == "ha_url")) is None
    assert await db_session.scalar(select(Settings.value).where(Settings.key == "ha_token")) is None


async def test_partial_restore_cannot_remove_operator_ca_verification(db_session):
    db_session.add_all(
        [
            Settings(key="ldap_bind_password", value="retained-bind-password"),
            Settings(key="ldap_ca_cert_path", value="/operator/ca.pem"),
        ]
    )
    await db_session.commit()
    service = GitRestoreService()
    payload = {"settings": {"ldap_ca_cert_path": ""}}
    preview, _ = await service._count_items(db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: payload})
    tally = _CategoryTally()
    await service._restore_settings(db_session, payload, True, tally)
    await db_session.commit()
    assert preview == tally.restored == 0
    rows = dict((await db_session.execute(select(Settings.key, Settings.value))).all())
    assert rows["ldap_ca_cert_path"] == "/operator/ca.pem" and rows["ldap_bind_password"] == "retained-bind-password"
