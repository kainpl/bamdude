"""The advertised AMS profile is a projection of the actual plan — never a mutation of it."""

import pytest

from backend.app.services import ams_backup_compatibility as compat
from backend.app.services.slot_assignment import build_slot_assignment

P1S = {"printer_model": "P1S", "nozzle_diameter": "0.4", "supports_user_preset": False}


async def _actual(db, **kw):
    return await build_slot_assignment(
        db, family_id="GFG99", color_rgba="FF0000FF", temp_overrides=(231, 261), **P1S, **kw
    )


def _project(db, actual, policy, **kw):
    args = dict(
        live_tray={"tag_uid": "0000000000000000", "tray_uuid": ""},
        spool_tag_uid=None,
        spool_tray_uuid=None,
        ams_id=0,
        material="PETG",
        extra_colors=None,
        **P1S,
    )
    args.update(kw)
    return compat.project_slot_assignment(db, actual=actual, policy=policy, **args)


@pytest.mark.asyncio
async def test_policy_off_returns_the_actual_plan_itself(db_session):
    actual = await _actual(db_session)
    p = await _project(db_session, actual, compat.BackupCompatibilityPolicy())
    assert p.advertised is actual and not p.projected and p.reasons == (compat.REASON_POLICY_OFF,)


@pytest.mark.asyncio
async def test_color_mode_replaces_tray_color_and_every_col(db_session):
    actual = await _actual(db_session, extra_colors="00FF00FF,0000FFFF")
    p = await _project(
        db_session, actual, compat.BackupCompatibilityPolicy(normalize_color=True, canonical_color_rgba="000000FF")
    )
    assert p.applied == ("color",)
    assert (p.advertised.tray_color, p.advertised.cols, p.advertised.ctype) == ("000000FF", [], 0)
    assert (actual.tray_color, actual.ctype) == ("FF0000FF", 1)  # the actual plan is untouched
    assert p.advertised.tray_info_idx == actual.tray_info_idx


@pytest.mark.asyncio
async def test_generic_mode_rebuilds_a_whole_generic_preset(db_session):
    actual = await build_slot_assignment(
        db_session, family_id="GFG99", color_rgba="FF0000FF", temp_overrides=(231, 261), **P1S
    )
    p = await _project(db_session, actual, compat.BackupCompatibilityPolicy(generic_base_material=True))
    assert p.applied == ("generic",)
    assert p.advertised.tray_info_idx == "GFG99" and p.advertised.setting_id.startswith("GFSG99")
    assert (p.advertised.nozzle_temp_min, p.advertised.nozzle_temp_max) != (231, 261)  # preset temps, not the spool's
    assert p.advertised.tray_color == "FF0000FF"  # colour mode off: colour stays real


@pytest.mark.asyncio
async def test_generic_only_keeps_a_gradient_spools_cols(db_session):
    """Colour mode off means the WHOLE colour stays real, gradient included.

    The caller need not hand the stops over twice: ``actual.cols`` already
    carries them, so a caller that omits ``extra_colors`` must not silently
    flatten a gradient tray to one colour."""
    actual = await _actual(db_session, extra_colors="00FF00FF,0000FFFF")
    p = await _project(db_session, actual, compat.BackupCompatibilityPolicy(generic_base_material=True))
    assert p.applied == ("generic",)
    assert p.advertised.cols == actual.cols and p.advertised.ctype == 1


@pytest.mark.asyncio
async def test_generic_mode_never_flattens_a_filled_material(db_session):
    actual = await build_slot_assignment(db_session, family_id="GFG99", material_override="PETG-CF", **P1S)
    p = await _project(
        db_session, actual, compat.BackupCompatibilityPolicy(generic_base_material=True), material="PETG-CF"
    )
    assert p.advertised is actual and p.reasons == (compat.REASON_BASE_MATERIAL,)


@pytest.mark.asyncio
async def test_rfid_slot_is_excluded_by_live_tray_and_by_spool(db_session):
    actual = await _actual(db_session)
    both = compat.BackupCompatibilityPolicy(normalize_color=True, generic_base_material=True)
    by_tray = await _project(db_session, actual, both, live_tray={"tag_uid": "A1B2C3D4E5F60718", "tray_uuid": ""})
    by_spool = await _project(db_session, actual, both, spool_tray_uuid="0123456789ABCDEF0123456789ABCDEF")
    assert by_tray.advertised is actual and by_tray.reasons == (compat.REASON_RFID,)
    assert by_spool.advertised is actual and by_spool.reasons == (compat.REASON_RFID,)


@pytest.mark.asyncio
async def test_external_slot_is_outside_the_mvp(db_session):
    actual = await _actual(db_session)
    p = await _project(db_session, actual, compat.BackupCompatibilityPolicy(normalize_color=True), ams_id=255)
    assert p.advertised is actual and p.reasons == (compat.REASON_EXTERNAL,)


@pytest.mark.asyncio
async def test_both_modes_apply_generic_then_color(db_session):
    actual = await _actual(db_session, extra_colors="00FF00FF")
    p = await _project(
        db_session, actual, compat.BackupCompatibilityPolicy(normalize_color=True, generic_base_material=True)
    )
    assert p.applied == ("generic", "color")
    assert (
        p.advertised.setting_id.startswith("GFSG99")
        and p.advertised.tray_color == "000000FF"
        and p.advertised.cols == []
    )


def test_from_dict_falls_back_to_black_on_a_bad_colour():
    policy = compat.BackupCompatibilityPolicy.from_dict({"normalize_color": True, "canonical_color_rgba": "nope"})
    assert policy.canonical_color_rgba == "000000FF" and policy.enabled


def test_from_printer_reads_the_namespace():
    class P:
        ams_policies = {"backup_compatibility": {"generic_base_material": True}}

    assert compat.BackupCompatibilityPolicy.from_printer(P()).generic_base_material is True
    assert compat.BackupCompatibilityPolicy.from_printer(object()).enabled is False


def test_kprofile_knob():
    class Proj:
        applied = ("generic",)

    assert compat.kprofile_allowed(Proj()) is (compat.GENERIC_MODE_KPROFILE == "actual")
