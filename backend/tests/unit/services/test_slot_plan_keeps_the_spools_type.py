"""A slot is configured from the spool: its type is never replaced by a guess (upstream #2902, 88e8ca81).

When a spool names no family, the builder has to choose the profile the slot is
told about. It used to take ``Generic <base>`` — the name cut at the first
hyphen or space — and with it that generic's TYPE, so a PLA Aero spool went out
as PLA and an ASA-GF spool as ASA. The slot then said a material the spool is
not, and everything reading the slot (routing compares nothing else under the
default base-material matching, AMS Backup groups by it) believed it.

Now the type is the spool's. The profile is a family of exactly that type when
the catalogue has one; when it has none, the base material's generic profile
lends its temperatures and the type is written as the spool gives it. The same
rule holds when a custom family is degraded to a system one for a printer
without user presets: the id changes, the type does not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.models.user_filament import UserFilamentFamily
from backend.app.services.slot_assignment import build_slot_assignment


def _spool(material: str) -> SimpleNamespace:
    """A spool with no family link, no RFID and no legacy slicer code."""
    return SimpleNamespace(
        filament_family_id=None,
        bambu_filament_id=None,
        slicer_filament=None,
        material=material,
        rgba="E0E0E0FF",
        extra_colors=None,
        nozzle_temp_min=None,
        nozzle_temp_max=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("material", "family_id", "tray_type"),
    [
        ("PLA Aero", "GFA11", "PLA-AERO"),  # a family of exactly that type exists
        ("PLA-AERO", "GFA11", "PLA-AERO"),
        ("ABS-GF", "GFB50", "ABS-GF"),
        ("TPU-AMS", "GFU98", "TPU-AMS"),
        ("PLA+", "GFL99", "PLA"),  # a qualifier on a known type: that type's generic
        ("PLA Matte", "GFL99", "PLA"),
        ("ASA-GF", "GFB98", "ASA-GF"),  # no ASA-GF anywhere: Generic ASA's profile, the spool's type
    ],
)
async def test_a_spool_without_a_family_keeps_its_own_type(db_session, material, family_id, tray_type):
    plan = await build_slot_assignment(db_session, spool=_spool(material), printer_model="P1S")

    assert (plan.tray_info_idx, plan.tray_type) == (family_id, tray_type)


@pytest.mark.asyncio
async def test_a_material_no_profile_can_stand_for_is_still_refused(db_session):
    with pytest.raises(ValueError):
        await build_slot_assignment(db_session, spool=_spool("Unobtainium"), printer_model="P1S")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("custom_type", "family_id", "tray_type"),
    [("PLA-AERO", "GFA11", "PLA-AERO"), ("ASA-GF", "GFB98", "ASA-GF")],
)
async def test_a_custom_family_degraded_for_a_printer_keeps_its_type(db_session, custom_type, family_id, tray_type):
    db_session.add(
        UserFilamentFamily(
            filament_id="P0f00aa1",
            ecosystem="bambu",
            alias=f"shop {custom_type}",
            vendor="shop",
            filament_type=custom_type,
            origin="cloud_bambu",
        )
    )
    await db_session.commit()

    plan = await build_slot_assignment(
        db_session, family_id="P0f00aa1", printer_model="A1 Mini", supports_user_preset=False
    )

    assert (plan.tray_info_idx, plan.tray_type) == (family_id, tray_type)
