"""A family the user created reaches the slot as the user's preset for that printer (audit D2, upstream a7b56333).

A spool's identity is its FAMILY — the spool form writes the family id and
nothing finer (the preset picker was removed on 2026-08-23). For a system family
the slot builder picks the catalogue preset made for the printer and nozzle. A
family the user created in the slicer — a P-hash the catalogue does not know —
had nothing to pick from, so its slot went out with an empty ``setting_id`` and
the generic 200–240 °C, although the user's cloud mirrors hold its presets, one
per printer it was made for ("3DPlast PETG Basic @Bambu Lab X2D 0.4 nozzle",
"… P1S …", "… A1 mini …" — the shape of a real install's data).

Upstream answered with a per-spool, per-model override table. Ours needs none:
the family is the identity, and each mirror says which printer it is for — its
parent (``base_ref``) or the "@<printer>" part of its name.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services.slot_assignment import build_slot_assignment

CUSTOM = "Pb66a7f2"


def _spool(family: str | None, *, slicer_filament: str | None = None, material: str = "PETG"):
    """A spool as the form saves it: the family id in both columns."""
    return SimpleNamespace(
        filament_family_id=family,
        slicer_filament=slicer_filament if slicer_filament is not None else family,
        bambu_filament_id=None,
        material=material,
        rgba="FF0000FF",
        extra_colors=None,
        nozzle_temp_min=None,
        nozzle_temp_max=None,
    )


def _mirror(cloud_id, name, family, *, base_ref=None, temps=(231, 251), source="cloud_bambu", **extra):
    return UserFilamentPreset(
        ecosystem="orca" if source == "cloud_orca" else "bambu",
        source=source,
        cloud_id=cloud_id,
        name=name,
        family_filament_id=family,
        base_ref=base_ref,
        filament_type="PETG",
        nozzle_temp_min=temps[0],
        nozzle_temp_max=temps[1],
        **extra,
    )


def _family(db, filament_id=CUSTOM, alias="3DPlast PETG Basic", filament_type="PETG"):
    db.add(
        UserFilamentFamily(
            filament_id=filament_id, ecosystem="bambu", alias=alias, filament_type=filament_type, origin="cloud_bambu"
        )
    )


@pytest.fixture
async def custom_family(db_session):
    """A 'Create filament' family: one preset per printer, the printer in the name."""
    _family(db_session)
    db_session.add_all(
        [
            _mirror("PFUSx2d", "3DPlast PETG Basic @Bambu Lab X2D 0.4 nozzle", CUSTOM, temps=(240, 260)),
            _mirror("PFUSp1s", "3DPlast PETG Basic @Bambu Lab P1S 0.4 nozzle", CUSTOM, temps=(235, 255)),
            _mirror("PFUSa1m", "3DPlast PETG Basic @Bambu Lab A1 mini 0.4 nozzle", CUSTOM, temps=(230, 250)),
        ]
    )
    await db_session.commit()


async def _plan(db, spool, model, nozzle="0.4", *, user_presets=True):
    return await build_slot_assignment(
        db, spool=spool, printer_model=model, nozzle_diameter=nozzle, supports_user_preset=user_presets
    )


# -- the family's own preset for each printer ---------------------------------


@pytest.mark.parametrize(
    ("model", "setting_id", "temps"),
    [("X2D", "PFUSx2d", (240, 260)), ("P1S", "PFUSp1s", (235, 255)), ("A1 Mini", "PFUSa1m", (230, 250))],
)
async def test_each_printer_gets_the_variant_made_for_it(db_session, custom_family, model, setting_id, temps):
    plan = await _plan(db_session, _spool(CUSTOM), model)
    assert (plan.tray_info_idx, plan.setting_id) == (CUSTOM, setting_id)
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == temps


async def test_no_variant_for_this_nozzle_names_no_foreign_preset(db_session, custom_family):
    """A 0.6 nozzle has no variant: the X2D 0.4 preset is not this printer
    profile, so none is named. The family still identifies the filament, and the
    same model's variant says what temperatures it prints at."""
    plan = await _plan(db_session, _spool(CUSTOM), "X2D", nozzle="0.6")
    assert (plan.tray_info_idx, plan.setting_id) == (CUSTOM, "")
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (240, 260)


async def test_a_printer_the_family_was_never_made_for_still_gets_its_temperatures(db_session, custom_family):
    plan = await _plan(db_session, _spool(CUSTOM), "X1C")
    assert plan.setting_id == ""
    assert plan.nozzle_temp_min in (230, 235, 240), "the filament's own, not the generic 200"


async def test_a_name_with_a_preset_suffix_is_matched_through_the_catalogue(db_session):
    _family(db_session, "Pc0ffee1", alias="Mine", filament_type="PLA")
    db_session.add_all(
        [
            _mirror("PFUSminex1c", "Mine @BBL X1C", "Pc0ffee1"),
            _mirror("PFUSminea1m", "Mine @BBL A1M", "Pc0ffee1", temps=(211, 222)),
        ]
    )
    await db_session.commit()

    plan = await _plan(db_session, _spool("Pc0ffee1", material="PLA"), "A1 Mini")
    assert plan.setting_id == "PFUSminea1m"
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (211, 222)


async def test_a_parent_preset_says_which_printer_it_is(db_session):
    """No "@" in the name — the parent (base_ref) decides."""
    _family(db_session, "Pbase001", alias="Parented", filament_type="PETG")
    db_session.add_all(
        [
            _mirror("PFUSpar1", "Parented one", "Pbase001", base_ref="GFSG99", temps=(241, 251)),
            _mirror("PFUSpar2", "Parented two", "Pbase001", base_ref="GFSG99_00", temps=(242, 252)),
        ]
    )
    await db_session.commit()

    plan = await _plan(db_session, _spool("Pbase001"), "A1 Mini")
    assert plan.setting_id == "PFUSpar2"


async def test_the_newest_of_two_variants_for_one_printer_wins(db_session):
    _family(db_session, "Pdup0001", alias="Dup", filament_type="PETG")
    db_session.add_all(
        [
            _mirror("PFUSold", "Dup @Bambu Lab P1S 0.4 nozzle", "Pdup0001", updated_time="2026-08-01 10:00:00"),
            _mirror("PFUSnew", "Dup @Bambu Lab P1S 0.4 nozzle", "Pdup0001", updated_time="2026-09-01 10:00:00"),
        ]
    )
    await db_session.commit()

    plan = await _plan(db_session, _spool("Pdup0001"), "P1S")
    assert plan.setting_id == "PFUSnew"


async def test_a_legacy_spool_linked_by_a_preset_id_gets_its_printers_variant(db_session, custom_family):
    """Saved before the family link existed: the X2D preset id in the column."""
    plan = await _plan(db_session, _spool(None, slicer_filament="PFUSx2d"), "P1S")
    assert (plan.tray_info_idx, plan.setting_id) == (CUSTOM, "PFUSp1s")


# -- where nothing changes ----------------------------------------------------


async def test_a_system_family_keeps_the_catalogue_variant(db_session):
    """The user's tweaked copies of Generic PETG are not this spool's identity —
    the spool is Generic PETG, and gets Generic PETG for its printer."""
    db_session.add(_mirror("PFUSkrp1s", "P1S Sunlu PETG (Krylo)", "GFG99", base_ref="GFSG99", temps=(236, 246)))
    await db_session.commit()

    assert (await _plan(db_session, _spool("GFG99"), "P1S")).setting_id == "GFSG99"
    assert (await _plan(db_session, _spool("GFG99"), "A1 Mini")).setting_id == "GFSG99_00"


async def test_a_printer_without_user_presets_gets_a_system_family(db_session, custom_family):
    plan = await _plan(db_session, _spool(CUSTOM), "X2D", user_presets=False)
    assert not plan.tray_info_idx.startswith("P")
    assert not plan.setting_id.startswith("PFUS")


async def test_orca_profiles_name_nothing_a_printer_knows(db_session):
    _family(db_session, "Porca001", alias="Orca only", filament_type="PETG")
    db_session.add(_mirror("uuid-orca-1", "Orca only @BBL X1C", "Porca001", source="cloud_orca"))
    await db_session.commit()

    plan = await _plan(db_session, _spool("Porca001"), "X1C")
    assert plan.setting_id == ""


async def test_an_explicit_preset_still_wins(db_session, custom_family):
    """Configure Slot's own pick is the operator's, and is sent as given."""
    plan = await build_slot_assignment(
        db_session, family_id=CUSTOM, preset_setting_id="PFUSx2d", printer_model="P1S", supports_user_preset=True
    )
    assert plan.setting_id == "PFUSx2d"


async def test_an_unknown_model_names_no_variant(db_session, custom_family):
    plan = await _plan(db_session, _spool(CUSTOM), None)
    assert plan.setting_id == ""
    assert plan.nozzle_temp_min in (230, 235, 240)
