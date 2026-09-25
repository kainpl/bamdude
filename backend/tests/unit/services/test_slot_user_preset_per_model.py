"""A spool's own cloud preset reaches the slot, in its printer model's variant (audit D2, upstream a7b56333).

A slicer preset belongs to a printer model: "3DPlast PETG Basic @Bambu Lab X2D
0.4 nozzle" and its "@Bambu Lab P1S 0.4 nozzle" twin are separate presets of one
filament. For SYSTEM families the slot builder has always picked the variant for
the printer from the catalogue. For a USER preset it did not look at all: the
spool form stores the family beside the chosen preset, the resolver answers
through the family, and the chosen preset was dropped — a custom filament went
out with an empty ``setting_id`` and the generic 200–240 °C, a tweaked copy of a
system preset with the system preset and its temperatures.

Upstream answered with a per-model override table the operator fills in. Ours
needs none: the family is the identity, and the cloud mirrors already say which
model each variant is for (``base_ref`` → the catalogue's compatible printers,
or the "@<printer>" part of the name). A sibling is taken only when it is
unambiguous — same family AND the same name once the "@…" part is removed —
because a system family such as Generic PETG holds the user's presets for
several different products.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services.slot_assignment import build_slot_assignment

CUSTOM = "Pb66a7f2"


def _spool(slicer_filament: str | None, family: str | None, material: str = "PETG"):
    return SimpleNamespace(
        filament_family_id=family,
        slicer_filament=slicer_filament,
        bambu_filament_id=None,
        material=material,
        rgba="FF0000FF",
        extra_colors=None,
        nozzle_temp_min=None,
        nozzle_temp_max=None,
    )


def _mirror(cloud_id, name, family, *, base_ref=None, temps=(231, 251), source="cloud_bambu", **extra):
    return UserFilamentPreset(
        ecosystem="orca" if source == "cloud_orca" else extra.pop("ecosystem", "bambu"),
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


@pytest.fixture
async def custom_family(db_session):
    """A 'Create filament' family: one preset per printer, the model in the name."""
    db_session.add(
        UserFilamentFamily(
            filament_id=CUSTOM,
            ecosystem="bambu",
            alias="3DPlast PETG Basic",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    db_session.add_all(
        [
            _mirror("PFUSx2d", "3DPlast PETG Basic @Bambu Lab X2D 0.4 nozzle", CUSTOM, temps=(240, 260)),
            _mirror("PFUSp1s", "3DPlast PETG Basic @Bambu Lab P1S 0.4 nozzle", CUSTOM, temps=(235, 255)),
            _mirror("PFUSa1m", "3DPlast PETG Basic @Bambu Lab A1 mini 0.4 nozzle", CUSTOM, temps=(230, 250)),
        ]
    )
    await db_session.commit()


@pytest.fixture
async def generic_copies(db_session):
    """Tweaked copies of Generic PETG — the family holds two different products."""
    db_session.add_all(
        [
            _mirror("PFUSkrp1s", "P1S Sunlu PETG (Krylo)", "GFG99", base_ref="GFSG99", temps=(236, 246)),
            _mirror("PFUSkra1m", "A1 Mini Sunlu PETG (Krylo)", "GFG99", base_ref="GFSG99_00", temps=(237, 247)),
            _mirror("PFUSsuna1m", "A1 Mini Sunlu PETG", "GFG99", base_ref="GFSG99_00", temps=(238, 248)),
        ]
    )
    await db_session.commit()


async def _plan(db, spool, model, nozzle="0.4", *, user_presets=True):
    return await build_slot_assignment(
        db, spool=spool, printer_model=model, nozzle_diameter=nozzle, supports_user_preset=user_presets
    )


# -- a custom family ----------------------------------------------------------


async def test_the_chosen_preset_reaches_its_own_model(db_session, custom_family):
    plan = await _plan(db_session, _spool("PFUSx2d", CUSTOM), "X2D")
    assert (plan.tray_info_idx, plan.setting_id) == (CUSTOM, "PFUSx2d")
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (240, 260)


async def test_another_model_gets_the_variant_made_for_it(db_session, custom_family):
    plan = await _plan(db_session, _spool("PFUSx2d", CUSTOM), "P1S")
    assert plan.setting_id == "PFUSp1s"
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (235, 255)


async def test_a_model_matched_by_display_name_variant(db_session, custom_family):
    """'A1 mini' is stored normalised; the preset names the printer 'A1 mini'."""
    plan = await _plan(db_session, _spool("PFUSx2d", CUSTOM), "A1 Mini")
    assert plan.setting_id == "PFUSa1m"


async def test_no_variant_for_this_nozzle_sends_no_foreign_preset(db_session, custom_family):
    """A 0.6 nozzle has no variant: the X2D's preset is not what this printer
    runs, so none is named — the family still identifies the filament, and the
    chosen preset's temperatures are the best there are."""
    plan = await _plan(db_session, _spool("PFUSx2d", CUSTOM), "X2D", nozzle="0.6")
    assert plan.tray_info_idx == CUSTOM
    assert plan.setting_id == ""
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (240, 260)


async def test_a_name_with_a_preset_suffix_is_matched_through_the_catalogue(db_session):
    db_session.add(
        UserFilamentFamily(
            filament_id="Pc0ffee1", ecosystem="bambu", alias="Mine", filament_type="PLA", origin="cloud_bambu"
        )
    )
    db_session.add_all(
        [
            _mirror("PFUSminex1c", "Mine @BBL X1C", "Pc0ffee1"),
            _mirror("PFUSminea1m", "Mine @BBL A1M", "Pc0ffee1", temps=(211, 222)),
        ]
    )
    await db_session.commit()

    plan = await _plan(db_session, _spool("PFUSminex1c", "Pc0ffee1", material="PLA"), "A1 Mini")
    assert plan.setting_id == "PFUSminea1m"
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (211, 222)


# -- tweaked copies of a system preset ---------------------------------------


async def test_a_tweaked_copy_keeps_its_own_preset_and_temperatures(db_session, generic_copies):
    plan = await _plan(db_session, _spool("PFUSkrp1s", "GFG99"), "P1S")
    assert (plan.tray_info_idx, plan.setting_id) == ("GFG99", "PFUSkrp1s")
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (236, 246)


async def test_a_copy_named_otherwise_is_not_its_sibling(db_session, generic_copies):
    """'A1 Mini Sunlu PETG (Krylo)' may well be the same roll's A1 preset, but the
    names differ by the model the user typed — and 'A1 Mini Sunlu PETG' is a
    different product in the same family. Guessing between them would put one
    spool's tuning on another; the model's system preset is the safe answer."""
    plan = await _plan(db_session, _spool("PFUSkrp1s", "GFG99"), "A1 Mini")
    assert plan.setting_id == "GFSG99_00"


async def test_a_same_named_copy_for_the_other_model_is_taken(db_session):
    db_session.add_all(
        [
            _mirror("PFUSsamep1s", "Sunlu PETG", "GFG99", base_ref="GFSG99", temps=(241, 251)),
            _mirror("PFUSsamea1m", "Sunlu PETG", "GFG99", base_ref="GFSG99_00", temps=(242, 252)),
        ]
    )
    await db_session.commit()

    plan = await _plan(db_session, _spool("PFUSsamep1s", "GFG99"), "A1 Mini")
    assert plan.setting_id == "PFUSsamea1m"
    assert (plan.nozzle_temp_min, plan.nozzle_temp_max) == (242, 252)


async def test_two_candidates_are_not_chosen_between(db_session):
    db_session.add_all(
        [
            _mirror("PFUSdupp1s", "Dup PETG", "GFG99", base_ref="GFSG99"),
            _mirror("PFUSdupa1", "Dup PETG", "GFG99", base_ref="GFSG99_00"),
            _mirror("PFUSdupa2", "Dup PETG", "GFG99", base_ref="GFSG99_00"),
        ]
    )
    await db_session.commit()

    plan = await _plan(db_session, _spool("PFUSdupp1s", "GFG99"), "A1 Mini")
    assert plan.setting_id == "GFSG99_00"


# -- where nothing changes ----------------------------------------------------


async def test_a_printer_without_user_presets_gets_the_system_preset(db_session, generic_copies, custom_family):
    tweaked = await _plan(db_session, _spool("PFUSkrp1s", "GFG99"), "P1S", user_presets=False)
    assert tweaked.setting_id == "GFSG99"
    custom = await _plan(db_session, _spool("PFUSx2d", CUSTOM), "X2D", user_presets=False)
    assert not custom.setting_id.startswith("PFUS")
    assert not custom.tray_info_idx.startswith("P")


async def test_a_stale_preset_of_another_family_is_ignored(db_session, custom_family):
    """The family was changed after the preset was picked: the family wins."""
    plan = await _plan(db_session, _spool("PFUSx2d", "GFG99"), "P1S")
    assert plan.tray_info_idx == "GFG99"
    assert plan.setting_id == "GFSG99"


async def test_an_orca_preset_changes_nothing(db_session):
    db_session.add(_mirror("uuid-orca-1", "Orca PETG", "GFG99", base_ref="Generic PETG @BBL A1M", source="cloud_orca"))
    await db_session.commit()

    plan = await _plan(db_session, _spool("uuid-orca-1", "GFG99"), "P1S")
    assert plan.setting_id == "GFSG99"


async def test_a_system_preset_pick_keeps_the_catalogue_variant(db_session):
    plan = await _plan(db_session, _spool("GFSG99_00", "GFG99"), "X2D")
    assert plan.setting_id == "GFSG99_15"


async def test_an_unknown_model_keeps_the_chosen_preset(db_session, custom_family):
    plan = await _plan(db_session, _spool("PFUSx2d", CUSTOM), None)
    assert plan.setting_id == "PFUSx2d"
