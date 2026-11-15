"""What a slot is told when the named filament cannot be used as named.

* Nylon / PA6 / PA12 (upstream 922f781a4, owner decision Р26): names the
  catalogue knows as no type, so ``build_slot_assignment`` raised — on assign the
  error was swallowed and NOTHING was published. They borrow Generic PA's
  profile; the slot keeps the spool's own type, as for every family-less spool.
  "Support" stays unresolved.
* A fallback to a generic profile is SAID (upstream 02e39b18b·3): the plan
  carries a code, and Configure Slot returns it instead of a bare "Configured".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.user_filament import UserFilamentFamily
from backend.app.services.slot_assignment import build_slot_assignment


def _spool(material: str) -> SimpleNamespace:
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
@pytest.mark.parametrize("material", ["Nylon", "NYLON", "PA6", "PA12"])
async def test_a_nylon_spool_borrows_generic_pa_and_keeps_its_own_type(db_session, material):
    plan = await build_slot_assignment(db_session, spool=_spool(material), printer_model="P1S")
    assert (plan.tray_info_idx, plan.tray_type) == ("GFN99", material)


@pytest.mark.asyncio
async def test_support_is_still_no_material(db_session):
    with pytest.raises(ValueError):
        await build_slot_assignment(db_session, spool=_spool("Support"), printer_model="P1S")


@pytest.mark.asyncio
async def test_a_family_that_resolves_is_not_degraded(db_session):
    plan = await build_slot_assignment(db_session, family_id="GFG99", material_override="PETG", printer_model="P1S")
    assert plan.degraded is None


@pytest.mark.asyncio
async def test_a_spool_that_names_no_family_is_not_degraded(db_session):
    """Its generic IS its profile — nothing the operator named was replaced."""
    plan = await build_slot_assignment(db_session, spool=_spool("PETG"), printer_model="P1S")
    assert plan.degraded is None


@pytest.mark.asyncio
async def test_a_named_family_nobody_knows_is_degraded(db_session):
    plan = await build_slot_assignment(db_session, family_id="Pdeadbee", material_override="PETG", printer_model="P1S")
    assert (plan.tray_info_idx, plan.degraded) == ("GFG99", "family_unresolved")


@pytest.mark.asyncio
async def test_a_user_preset_on_a_printer_that_takes_none_is_degraded(db_session):
    db_session.add(
        UserFilamentFamily(
            filament_id="P0f00aa1",
            ecosystem="bambu",
            alias="shop PETG",
            vendor="shop",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    await db_session.commit()
    plan = await build_slot_assignment(
        db_session, family_id="P0f00aa1", material_override="PETG", printer_model="P1S", supports_user_preset=False
    )
    assert (plan.tray_info_idx, plan.degraded) == ("GFG99", "user_preset_unsupported")


_PARAMS = {
    "tray_type": "PETG",
    "tray_sub_brands": "shop PETG",
    "tray_color": "FFFFFFFF",
    "nozzle_temp_min": 0,
    "nozzle_temp_max": 0,
}


async def _configure(async_client, printer, **params):
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    client.extrusion_cali_sel.return_value = True
    with patch("backend.app.api.routes.printers.printer_manager") as manager:
        manager.ensure_fresh_connection = AsyncMock(return_value=True)
        manager.get_client.return_value = client
        manager.get_status.return_value = None
        manager.get_model.return_value = "P1S"
        response = await async_client.post(
            f"/api/v1/printers/{printer.id}/slots/0/1/configure", params={**_PARAMS, **params}
        )
    assert response.status_code == 200, response.text
    return client, response.json()


@pytest.mark.asyncio
async def test_configure_says_when_the_slot_got_a_generic_instead(async_client, printer_factory):
    printer = await printer_factory(name="P1S", model="P1S")
    client, body = await _configure(async_client, printer, tray_info_idx="Pdeadbee")
    assert client.ams_set_filament_setting.call_args.kwargs["tray_info_idx"] == "GFG99"
    assert body["warnings"] == ["family_unresolved"]


@pytest.mark.asyncio
async def test_configure_takes_a_setting_id_for_the_filament_it_names(async_client, printer_factory):
    """Upstream d6de7aa69·1: an API client's setting id used to degrade to the
    material's Generic ("GFSA00" + PLA → Generic PLA, not Bambu PLA Basic)."""
    printer = await printer_factory(name="P1S", model="P1S")
    client, body = await _configure(async_client, printer, tray_info_idx="GFSA00", tray_type="PLA")
    assert client.ams_set_filament_setting.call_args.kwargs["tray_info_idx"] == "GFA00"
    assert body["warnings"] == []


# --- a spool ALREADY linked to a family minted under an OrcaSlicer "OF…" id ------------------
# The sync only marks such a family orphaned (out of the picker); the spool still names it.


def _family_spool(family_id: str, material: str = "PETG") -> SimpleNamespace:
    spool = _spool(material)
    spool.filament_family_id = family_id
    return spool


async def _orca_family(db, filament_id: str) -> None:
    db.add(
        UserFilamentFamily(
            filament_id=filament_id,
            ecosystem="orca",
            alias="Bambu PETG HF",
            vendor="Bambu Lab",
            filament_type="PETG",
            origin="cloud_orca",
            orphaned=True,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_a_spool_linked_to_an_orca_id_family_is_published_under_bambus_id(db_session):
    """OF0UJcb6 is OrcaSlicer's id for Bambu PETG HF (GFG60). Sent as it was, the slot read back blank."""
    await _orca_family(db_session, "OF0UJcb6")
    plan = await build_slot_assignment(db_session, spool=_family_spool("OF0UJcb6"), printer_model="P1S")
    assert (plan.tray_info_idx, plan.tray_type, plan.degraded) == ("GFG60", "PETG", None)


@pytest.mark.asyncio
async def test_its_k_lookup_keys_on_the_same_translated_id(db_session):
    from backend.app.services.calibration_service import derive_effective_filament_id

    await _orca_family(db_session, "OF0UJcb6")
    assert await derive_effective_filament_id(spool=_family_spool("OF0UJcb6"), db=db_session) == "GFG60"


@pytest.mark.asyncio
async def test_an_orca_id_the_table_lacks_goes_out_as_the_materials_generic_and_says_so(db_session):
    await _orca_family(db_session, "OFzzzzzz")
    plan = await build_slot_assignment(db_session, spool=_family_spool("OFzzzzzz"), printer_model="P1S")
    assert (plan.tray_info_idx, plan.tray_type, plan.degraded) == ("GFG99", "PETG", "family_unresolved")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spool",
    [
        _family_spool("GFA00", "PLA"),
        _family_spool("GFG99"),
        _family_spool("P0f00aa1"),
        _spool("PETG"),
        _spool("PLA Aero"),
        _spool("Nylon"),
    ],
    ids=["bambu-pla-basic", "generic-petg", "user-family", "no-family", "aero", "nylon"],
)
async def test_every_other_spool_gets_the_plan_it_got_before(db_session, spool):
    """GUARD 2: with the translation switched off (identity) the plan and the K key are the same."""
    from backend.app.services import slot_assignment
    from backend.app.services.calibration_service import derive_effective_filament_id

    db_session.add(
        UserFilamentFamily(
            filament_id="P0f00aa1",
            ecosystem="bambu",
            alias="shop PETG",
            vendor="shop",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    await db_session.commit()
    plan = await build_slot_assignment(db_session, spool=spool, printer_model="P1S")
    key = await derive_effective_filament_id(spool=spool, db=db_session)
    # One module object: the patch reaches both readers.
    with patch.object(slot_assignment.catalog, "bambu_filament_id", lambda value: value):
        assert await build_slot_assignment(db_session, spool=spool, printer_model="P1S") == plan
        assert await derive_effective_filament_id(spool=spool, db=db_session) == key
