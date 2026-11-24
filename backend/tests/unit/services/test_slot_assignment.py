"""The single slot-assignment builder: BS-shaped payloads from the family
catalog — versioned setting_id, per-printer temps, cols/ctype, the
support_user_preset gate."""

import pytest

from backend.app.models.user_filament import UserFilamentFamily
from backend.app.services.slot_assignment import build_slot_assignment


@pytest.mark.asyncio
async def test_system_family_payload_is_bs_shaped(db_session):
    plan = await build_slot_assignment(
        db_session,
        family_id="GFG99",
        printer_model="A1 Mini",
        nozzle_diameter="0.4",
        color_rgba="FF0000FF",
    )
    assert plan.tray_info_idx == "GFG99"
    assert plan.setting_id.startswith("GFSG99")  # versioned, from the catalog — no string munging
    assert plan.tray_type == "PETG"
    assert plan.nozzle_temp_min is not None and plan.nozzle_temp_max is not None
    assert plan.cols == [] and plan.ctype == 0
    assert plan.warnings == []


@pytest.mark.asyncio
async def test_custom_family_gated_by_support_user_preset(db_session):
    db_session.add(
        UserFilamentFamily(
            filament_id="P122e532",
            ecosystem="bambu",
            alias="test PETG Basic",
            vendor="test",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    await db_session.commit()

    allowed = await build_slot_assignment(
        db_session, family_id="P122e532", printer_model="A1 Mini", supports_user_preset=True
    )
    assert allowed.tray_info_idx == "P122e532"

    gated = await build_slot_assignment(
        db_session, family_id="P122e532", printer_model="A1 Mini", supports_user_preset=False
    )
    assert gated.tray_info_idx == "GFG99"  # degraded to generic of same type
    assert gated.warnings  # and it says so


@pytest.mark.asyncio
async def test_multicolour_spool_gets_cols_and_ctype(db_session):
    plan = await build_slot_assignment(
        db_session,
        family_id="GFA00",
        printer_model="A1 Mini",
        color_rgba="FF0000FF",
        extra_colors="00FF00FF,0000FFFF",
    )
    assert plan.cols == ["FF0000FF", "00FF00FF", "0000FFFF"]
    assert plan.ctype == 1


@pytest.mark.asyncio
async def test_spool_temp_overrides_win(db_session):
    plan = await build_slot_assignment(
        db_session, family_id="GFG99", printer_model="A1 Mini", temp_overrides=(231, 261)
    )
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (231, 261)


@pytest.mark.asyncio
async def test_custom_family_k_profiles_are_matchable(db_session):
    """P-hash spool -> resolve_spool -> the SAME id the printer's K table uses.
    Regression for the flattening bug (custom families collapsed to generic)."""
    from types import SimpleNamespace

    from backend.app.services.calibration_service import derive_effective_filament_id

    db_session.add(
        UserFilamentFamily(filament_id="P122e532", ecosystem="bambu", alias="test PETG Basic", origin="cloud_bambu")
    )
    await db_session.commit()
    spool = SimpleNamespace(filament_family_id="P122e532", bambu_filament_id=None, slicer_filament="PFUS_CUSTOM_ROOT")
    assert await derive_effective_filament_id(spool=spool, db=db_session) == "P122e532"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family_id", "material", "wire_type"),
    [
        ("GFS00", "PLA", "PLA-S"),
        ("GFS01", "PA", "PA-S"),
        ("GFS05", "PLA", "PLA-S"),
        ("GFS04", "PVA", "PVA"),
        ("GFS06", "ABS", "ABS"),
        ("GFS06", "ABS-S", "ABS"),
        ("GFA00", "PLA", "PLA"),
    ],
)
async def test_selected_family_uses_the_bambu_studio_slot_type(db_session, family_id, material, wire_type):
    manual = await build_slot_assignment(db_session, family_id=family_id, material_override=material)
    spooled = await build_slot_assignment(db_session, stored_preset=family_id, material_override=material)
    assert (manual.tray_info_idx, manual.tray_type) == (family_id, wire_type)
    assert (spooled.tray_info_idx, spooled.tray_type, spooled.from_stored_preset) == (family_id, wire_type, True)


@pytest.mark.asyncio
async def test_support_slot_preserves_the_bound_preset_for_suffixed_material(db_session):
    plan = await build_slot_assignment(db_session, stored_preset="GFS02", material_override="PLA-S")
    assert (plan.tray_info_idx, plan.tray_type, plan.from_stored_preset) == ("GFS02", "PLA-S", True)
    unrelated = await build_slot_assignment(db_session, stored_preset="GFS02", material_override="PETG")
    assert (unrelated.tray_info_idx, unrelated.tray_type, unrelated.from_stored_preset) == ("GFG99", "PETG", False)


@pytest.mark.asyncio
async def test_support_family_requirements_keep_the_slicer_wire_types(db_session):
    from backend.app.services.filament_intake import enrich_family_filament_rows

    rows = await enrich_family_filament_rows(
        db_session,
        [
            {"tray_info_idx": "GFS05", "filament_type": "PLA-S"},
            {"tray_info_idx": "GFS03", "filament_type": "PA-S"},
            {"tray_info_idx": "GFA00", "filament_type": "PLA"},
        ],
    )
    assert [row["filament_type"] for row in rows] == ["PLA-S", "PA-S", "PLA"]


@pytest.mark.asyncio
@pytest.mark.parametrize("material", ["PLA", "PA", "PLA-S", "PA-S"])
async def test_custom_family_keeps_its_declared_type_in_slot_and_3mf(db_session, material):
    from backend.app.services.filament_intake import enrich_family_filament_rows

    family_id = "P122e532"
    db_session.add(
        UserFilamentFamily(
            filament_id=family_id,
            ecosystem="bambu",
            alias="PLA Support Interface Test",
            vendor="test",
            filament_type=material,
            origin="cloud_bambu",
        )
    )
    await db_session.commit()

    plan = await build_slot_assignment(
        db_session, family_id=family_id, printer_model="A1 Mini", supports_user_preset=True
    )
    rows = await enrich_family_filament_rows(db_session, [{"tray_info_idx": family_id, "filament_type": material}])

    assert ((plan.tray_info_idx, plan.tray_type), rows[0]["filament_type"]) == ((family_id, material), material)
