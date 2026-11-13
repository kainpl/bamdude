"""Partial settings restore uses the owning editor contract, never arbitrary KV."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.models.settings import Settings
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services.git_restore import SETTINGS_PATH, GitRestoreService, _CategoryTally


async def apply(db, values, *, overwrite=True):
    tally = _CategoryTally()
    await GitRestoreService()._restore_settings(db, {"settings": values}, overwrite, tally)
    await db.flush()
    return tally


def test_explicit_allowlist_contains_only_current_editor_fields_and_accepts_their_defaults():
    from backend.app.schemas.settings import AppSettings, AppSettingsUpdate
    from backend.app.services.git_restore import _RESTORABLE_SETTING_KEYS, _restore_setting_value

    defaults = AppSettings()
    assert _RESTORABLE_SETTING_KEYS.issubset(AppSettings.model_fields.keys() & AppSettingsUpdate.model_fields.keys())
    for key in _RESTORABLE_SETTING_KEYS:
        _restore_setting_value(key, getattr(defaults, key))


@pytest.mark.asyncio
async def test_classifier_rejects_unknown_runtime_bad_types_masks_and_auth_without_echo(db_session):
    values = {
        "foreign_key": "private-canary",
        "ha_token_from_env": "true",
        "currency": "******",
        "archive_3mf_retention_days": "0",
        "session_max_hours": "720",
        "mqtt_password": "secret",
    }
    service = GitRestoreService()
    count, detail = await service._count_items(
        db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}}
    )
    assert count == 0
    assert detail is not None
    tally = await apply(db_session, values)
    assert tally.restored == tally.skipped == tally.failed == 0
    assert "private-canary" not in str(tally.notes) + str(detail)
    stored = (await db_session.execute(select(Settings).where(Settings.key.in_(values)))).scalars().all()
    assert stored == []
    assert {note["code"] for note in tally.notes} >= {
        "settingsUnknownSkipped",
        "settingsRuntimeSkipped",
        "settingsInvalidSkipped",
        "settingsMaskedSkipped",
        "settingsAuthSkipped",
    }


@pytest.mark.asyncio
async def test_current_schema_ranges_nullable_json_and_canonical_storage(db_session):
    values = {
        "currency": "EUR",
        "check_updates": True,
        "include_beta_updates": "TRUE",
        "log_retention_days": "12",
        "open_in_slicer": None,
        "default_printer_id": None,
        "ams_temp_alarm": None,
        "stagger_group_tag_ids": "[3, 1, 3]",
        "mqtt_enabled": "on",
        "energy_cost_per_kwh": "NaN",
        "ha_url": None,
    }
    count, _ = await GitRestoreService()._count_items(
        db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}}
    )
    tally = await apply(db_session, values)
    rows = {
        r.key: r.value for r in (await db_session.execute(select(Settings).where(Settings.key.in_(values)))).scalars()
    }
    assert count == tally.restored == 8
    assert rows == {
        "currency": "EUR",
        "check_updates": "true",
        "include_beta_updates": "true",
        "log_retention_days": "12",
        "open_in_slicer": "None",
        "default_printer_id": "None",
        "ams_temp_alarm": "None",
        "stagger_group_tag_ids": "[1, 3]",
    }


@pytest.mark.asyncio
async def test_stale_preview_rereads_local_destination_and_preserves_local_data_routes(db_session):
    db_session.add(Settings(key="spoolman_url", value="http://local:7912"))
    await db_session.commit()
    values = {"spoolman_url": "http://local:7912/", "currency": "EUR", "local_backup_path": "D:/exfil"}
    service = GitRestoreService()
    before, _ = await service._count_items(db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}})
    row = (await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
    row.value = "http://new-local:7912"
    await db_session.commit()
    tally = await apply(db_session, values)
    assert before == 2  # canonical destination no-op plus ordinary setting
    assert tally.restored == 1
    assert row.value == "http://new-local:7912"
    assert not (await db_session.execute(select(Settings).where(Settings.key == "local_backup_path"))).scalars().all()


@pytest.mark.asyncio
async def test_effective_env_destination_noop_is_not_copied_to_db(db_session):
    with patch("backend.app.core.config.settings.slicer_api_url", "http://local:3003"):
        tally = await apply(db_session, {"orcaslicer_api_url": "http://LOCAL:3003/"})
    assert tally.restored == 0 and tally.skipped == 1
    assert not (await db_session.execute(select(Settings).where(Settings.key == "orcaslicer_api_url"))).scalars().all()


@pytest.mark.asyncio
async def test_private_model_destination_selectors_cannot_be_changed_by_partial_restore(db_session):
    selectors = {"preferred_slicer": "orcaslicer", "slice_engine": "desktop", "use_slicer_api": "true"}
    count, detail = await GitRestoreService()._count_items(
        db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": selectors}}
    )
    assert count == 0 and detail.params["destination"] == 3
    tally = await apply(db_session, selectors)
    assert tally.restored == tally.skipped == 0
    assert not (await db_session.execute(select(Settings).where(Settings.key.in_(selectors)))).scalars().all()
    tally = await apply(
        db_session, {"preferred_slicer": "bambu_studio", "slice_engine": "sidecar", "use_slicer_api": False}
    )
    assert tally.restored == 0 and tally.skipped == 3
    assert not (await db_session.execute(select(Settings).where(Settings.key.in_(selectors)))).scalars().all()


@pytest.mark.asyncio
async def test_mqtt_reconfigure_uses_shared_latest_snapshot_owner(db_session):
    with patch("backend.app.api.routes.settings._apply_mqtt_runtime", new_callable=AsyncMock) as configure:
        await GitRestoreService()._reconfigure_mqtt_relay(db_session, {"mqtt_enabled"}, _CategoryTally())
    configure.assert_awaited_once_with(db_session)


@pytest.mark.asyncio
async def test_all_frozen_destinations_refuse_new_route_and_keep_ordinary_preferences(db_session):
    values = {
        "spoolman_url": "http://elsewhere",
        "orcaslicer_api_url": "http://elsewhere:3003",
        "bambu_studio_api_url": "http://elsewhere:3001",
        "external_url": "http://elsewhere",
        "docker_compose_dir": "D:/elsewhere",
        "local_backup_path": "D:/elsewhere",
        "zigbee_transport": "usb",
        "zigbee_path": "COM99",
        "mqtt_topic_prefix": "other",
        "virtual_printer_mode": "immediate",
        "currency": "GBP",
    }
    tally = await apply(db_session, values)
    assert tally.restored == 1
    assert next(n for n in tally.notes if n["code"] == "settingsDestinationSkipped")["params"]["count"] == 10
    rows = {
        r.key: r.value for r in (await db_session.execute(select(Settings).where(Settings.key.in_(values)))).scalars()
    }
    assert rows == {"currency": "GBP"}


@pytest.mark.asyncio
async def test_apply_waits_for_anchor_then_reclassifies_fresh_committed_destination(tmp_path):
    from backend.app.core.credential_config import CONFIG_GUARD_KEY, lock_credential_config

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restore.sqlite'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Settings.__table__.create)
        async with sessions() as writer, sessions() as restore:
            writer.add_all(
                [Settings(key=CONFIG_GUARD_KEY, value="1"), Settings(key="spoolman_url", value="http://first")]
            )
            await writer.commit()
            service = GitRestoreService()
            values = {"spoolman_url": "http://first", "currency": "EUR"}
            count, _ = await service._count_items(
                restore, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}}
            )
            assert count == 2
            await restore.rollback()
            await lock_credential_config(writer)
            row = (await writer.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
            row.value = "http://second"
            attempted = asyncio.Event()

            async def observed_lock(db):
                attempted.set()
                await lock_credential_config(db)

            with patch("backend.app.core.credential_config.lock_credential_config", observed_lock):
                task = asyncio.create_task(apply(restore, values))
                await asyncio.wait_for(attempted.wait(), 2)
                await writer.commit()
                tally = await asyncio.wait_for(task, 5)
            await restore.commit()
            assert tally.restored == 1
            assert next(n for n in tally.notes if n["code"] == "settingsDestinationSkipped")["params"] == {"count": 1}
            await writer.refresh(row)
            assert row.value == "http://second"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_apply_releases_checkout_before_outbound_and_retains_current_password(db_session):
    db_session.add_all(
        [
            Settings(key="mqtt_enabled", value="true"),
            Settings(key="mqtt_broker", value="current.local"),
            Settings(key="mqtt_password", value="current-secret"),
        ]
    )
    await db_session.commit()

    async def outbound(snapshot):
        assert not db_session.in_transaction()
        assert snapshot["mqtt_broker"] == "current.local" and snapshot["mqtt_password"] == "current-secret"

    with patch("backend.app.services.mqtt_relay.mqtt_relay.configure", side_effect=outbound) as configure:
        await GitRestoreService()._reconfigure_mqtt_relay(db_session, {"mqtt_enabled"}, _CategoryTally())
    configure.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit",
    [
        {"forecast_operator_absence_start": "08:00"},
        {"forecast_operator_absence_end": "23:00"},
    ],
)
async def test_operator_equal_boundary_is_invalid_without_refusing_unrelated_preferences(db_session, edit):
    values = {**edit, "currency": "EUR"}
    count, detail = await GitRestoreService()._count_items(
        db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}}
    )
    assert count == 1 and detail.params["invalid"] == 1
    tally = await apply(db_session, values)
    assert tally.restored == 1
    assert next(n for n in tally.notes if n["code"] == "settingsInvalidSkipped")["params"] == {"count": 1}
    assert not (await db_session.execute(select(Settings).where(Settings.key.in_(edit)))).scalars().all()


@pytest.mark.asyncio
async def test_operator_valid_paired_update_is_accepted(db_session):
    values = {"forecast_operator_absence_start": "08:00", "forecast_operator_absence_end": "17:00"}
    count, detail = await GitRestoreService()._count_items(
        db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}}
    )
    assert count == 2 and detail is None
    tally = await apply(db_session, values)
    assert tally.restored == 2
    assert {
        r.key: r.value for r in (await db_session.execute(select(Settings).where(Settings.key.in_(values)))).scalars()
    } == values


@pytest.mark.asyncio
async def test_operator_apply_rereads_changed_local_counterpart_after_preview(db_session):
    end = Settings(key="forecast_operator_absence_end", value="08:00")
    db_session.add(end)
    await db_session.commit()
    values = {"forecast_operator_absence_start": "10:00", "currency": "EUR"}
    service = GitRestoreService()
    count, _ = await service._count_items(db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}})
    assert count == 2
    await db_session.execute(
        update(Settings).where(Settings.id == end.id).values(value="10:00").execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert end.value == "08:00"  # deliberately stale identity-map object
    tally = await apply(db_session, values)
    assert tally.restored == 1 and end.value == "10:00"
    assert (
        not (await db_session.execute(select(Settings).where(Settings.key == "forecast_operator_absence_start")))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_operator_overwrite_off_does_not_turn_valid_backup_pair_into_invalid_partial_write(db_session):
    db_session.add(Settings(key="forecast_operator_absence_end", value="08:00"))
    await db_session.commit()
    values = {"forecast_operator_absence_start": "08:00", "forecast_operator_absence_end": "17:00", "currency": "EUR"}
    tally = await apply(db_session, values, overwrite=False)
    assert tally.restored == 1 and tally.skipped == 1
    rows = {
        r.key: r.value for r in (await db_session.execute(select(Settings).where(Settings.key.in_(values)))).scalars()
    }
    assert rows == {"forecast_operator_absence_end": "08:00", "currency": "EUR"}


@pytest.mark.asyncio
async def test_operator_all_overwrite_skips_leave_invalid_legacy_pair_untouched(db_session):
    db_session.add_all(
        [
            Settings(key="forecast_operator_absence_start", value="08:00"),
            Settings(key="forecast_operator_absence_end", value="08:00"),
        ]
    )
    await db_session.commit()
    tally = await apply(
        db_session,
        {"forecast_operator_absence_start": "09:00", "forecast_operator_absence_end": "17:00", "currency": "EUR"},
        overwrite=False,
    )
    assert tally.restored == 1 and tally.skipped == 2 and not tally.notes


@pytest.mark.asyncio
async def test_operator_classifier_matches_actual_owning_put_with_local_pair(async_client, db_session):
    response = await async_client.put("/api/v1/settings/", json={"forecast_operator_absence_start": "08:00"})
    assert response.status_code == 422
    response = await async_client.put(
        "/api/v1/settings/",
        json={
            "forecast_operator_absence_start": "09:00",
            "forecast_operator_absence_end": "18:00",
        },
    )
    assert response.status_code == 200
    for edit in ({"forecast_operator_absence_start": "18:00"}, {"forecast_operator_absence_end": "09:00"}):
        response = await async_client.put("/api/v1/settings/", json=edit)
        assert response.status_code == 422
        count, detail = await GitRestoreService()._count_items(
            db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": {**edit, "currency": "EUR"}}}
        )
        assert count == 1 and detail.params["invalid"] == 1
        tally = await apply(db_session, {**edit, "currency": "EUR"})
        assert tally.restored == 1
    rows = {
        r.key: r.value
        for r in (
            await db_session.execute(
                select(Settings).where(
                    Settings.key.in_({"forecast_operator_absence_start", "forecast_operator_absence_end"})
                )
            )
        ).scalars()
    }
    assert rows == {"forecast_operator_absence_start": "09:00", "forecast_operator_absence_end": "18:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("already_enabled", [False, True])
async def test_nonproxy_vp_enable_needs_local_code_even_when_backup_omits_it(db_session, already_enabled):
    if already_enabled:
        db_session.add(Settings(key="virtual_printer_enabled", value="true"))
        await db_session.commit()
    values = {"virtual_printer_enabled": "true", "currency": "EUR"}
    count, detail = await GitRestoreService()._count_items(
        db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}}
    )
    assert count == 1 and detail.params["companion"] == 1
    tally = await apply(db_session, values)
    assert tally.restored == 1
    assert next(n for n in tally.notes if n["code"] == "settingsCompanionSkipped")["params"]["count"] == 1


@pytest.mark.asyncio
async def test_vp_proxy_and_retained_code_keep_owning_editor_semantics(db_session):
    db_session.add(Settings(key="virtual_printer_mode", value="proxy"))
    await db_session.commit()
    tally = await apply(db_session, {"virtual_printer_enabled": True, "virtual_printer_access_code": "backup-secret"})
    assert tally.restored == 1  # proxy needs no local credential, even if backup contains a refused one
    mode = (await db_session.execute(select(Settings).where(Settings.key == "virtual_printer_mode"))).scalar_one()
    mode.value = "file_manager"
    db_session.add(Settings(key="virtual_printer_access_code", value="12345678"))
    await db_session.commit()
    tally = await apply(db_session, {"virtual_printer_enabled": True})
    assert tally.restored == 1


@pytest.mark.asyncio
async def test_backup_cannot_use_refused_proxy_mode_to_bypass_local_vp_code_requirement(db_session):
    tally = await apply(
        db_session, {"virtual_printer_enabled": True, "virtual_printer_mode": "proxy", "currency": "EUR"}
    )
    assert tally.restored == 1
    assert {n["code"] for n in tally.notes} == {"settingsCompanionSkipped", "settingsDestinationSkipped"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,local,allowed",
    [
        ("ha_enabled", {"ha_token": "retained"}, False),
        ("ha_enabled", {"ha_url": "http://ha.local"}, False),
        ("ha_enabled", {"ha_url": "http://ha.local", "ha_token": "retained"}, True),
        ("mqtt_enabled", {"mqtt_username": "account"}, False),
        ("mqtt_enabled", {}, True),
        ("mqtt_enabled", {"mqtt_username": "account", "mqtt_password": "retained"}, True),
        ("prometheus_enabled", {"prometheus_token": "   "}, False),
        ("prometheus_enabled", {"prometheus_token": "retained"}, True),
        ("virtual_printer_enabled", {}, False),
        ("virtual_printer_enabled", {"virtual_printer_mode": "proxy"}, True),
        ("virtual_printer_archive_name_source", {"virtual_printer_enabled": "true"}, False),
    ],
)
async def test_retained_integration_predicates_match_actual_owning_put(
    async_client, db_session, monkeypatch, key, local, allowed
):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setattr("backend.app.api.routes.settings._apply_mqtt_runtime", AsyncMock())
    db_session.add_all([Settings(key=k, value=v) for k, v in local.items()])
    await db_session.commit()
    edit = {key: "filename" if key == "virtual_printer_archive_name_source" else True}
    response = await async_client.put("/api/v1/settings/", json=edit)
    assert response.status_code == (200 if allowed else 400)
    values = {**edit, "currency": "EUR"}
    count, detail = await GitRestoreService()._count_items(
        db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}}
    )
    assert count == (2 if allowed else 1)
    if not allowed:
        assert detail.params["companion"] == 1
    tally = await apply(db_session, values)
    assert tally.restored == count
    retained = {
        r.key: r.value for r in (await db_session.execute(select(Settings).where(Settings.key.in_(local)))).scalars()
    }
    assert retained == local


@pytest.mark.asyncio
async def test_owning_resolver_unavailability_is_retryable_not_a_companion_refusal(db_session):
    with (
        patch(
            "backend.app.api.routes.settings.resolve_settings_credentials",
            AsyncMock(side_effect=HTTPException(503, "temporary")),
        ),
        pytest.raises(HTTPException) as unavailable,
    ):
        await GitRestoreService()._plan_settings(db_session, {"mqtt_enabled": True})
    assert unavailable.value.status_code == 503


@pytest.mark.asyncio
async def test_actual_toggle_edit_rereads_changed_cached_credential_context(db_session):
    username = Settings(key="mqtt_username", value="")
    db_session.add(username)
    await db_session.commit()
    values = {"mqtt_enabled": True, "currency": "EUR"}
    count, _ = await GitRestoreService()._count_items(
        db_session, RestoreCategory.SETTINGS, {SETTINGS_PATH: {"settings": values}}
    )
    assert count == 2
    await db_session.execute(
        update(Settings)
        .where(Settings.id == username.id)
        .values(value="account")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert username.value == ""
    tally = await apply(db_session, values)
    assert tally.restored == 1 and username.value == "account"
    assert not (await db_session.execute(select(Settings).where(Settings.key == "mqtt_enabled"))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite", [True, False])
async def test_vp_group_uses_complete_actual_edits_after_overwrite_skips(db_session, overwrite):
    db_session.add(Settings(key="virtual_printer_enabled", value="true"))
    await db_session.commit()
    values = {"virtual_printer_enabled": False, "virtual_printer_archive_name_source": "filename", "currency": "EUR"}
    tally = await apply(db_session, values, overwrite=overwrite)
    rows = {
        r.key: r.value for r in (await db_session.execute(select(Settings).where(Settings.key.in_(values)))).scalars()
    }
    if overwrite:
        assert tally.restored == 3 and not tally.notes
        assert rows == {
            "virtual_printer_enabled": "false",
            "virtual_printer_archive_name_source": "filename",
            "currency": "EUR",
        }
    else:
        assert tally.restored == 1 and tally.skipped == 1
        assert rows == {"virtual_printer_enabled": "true", "currency": "EUR"}
        assert next(n for n in tally.notes if n["code"] == "settingsCompanionSkipped")["params"]["count"] == 1
