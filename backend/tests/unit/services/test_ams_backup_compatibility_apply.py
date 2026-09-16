"""Bulk apply: preview publishes nothing; apply re-advertises only what the policy projects; the overlay rebuilds once."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_k_profile import SpoolmanKProfile
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services import ams_advertised_overlay as overlay, ams_backup_compatibility_apply as bulk

MANUAL = {"tag_uid": "0000000000000000", "tray_uuid": "", "state": 11, "exists": True}
SPOOLMAN_SPOOL = 77
# What Spoolman answers for it — ``_map_spoolman_spool`` is the real mapper.
SPOOLMAN_RAW = {
    "id": SPOOLMAN_SPOOL,
    "used_weight": 0,
    "filament": {
        "material": "PETG",
        "name": "PETG HF",
        "color_hex": "00FF00",
        "weight": 1000,
        "vendor": {"name": "Bambu Lab"},
    },
}


async def _farm(db_session):
    printer = Printer(
        name="P1S",
        serial_number="01P00A000000003",
        ip_address="192.168.1.52",
        access_code="12345678",
        model="P1S",
        ams_policies={"backup_compatibility": {"normalize_color": True}},
    )
    red = Spool(material="PETG", rgba="FF0000FF", filament_family_id="GFG99", label_weight=1000)
    rfid = Spool(
        material="PETG",
        rgba="0000FFFF",
        filament_family_id="GFG99",
        label_weight=1000,
        tray_uuid="0123456789ABCDEF0123456789ABCDEF",
    )
    db_session.add_all([printer, red, rfid])
    await db_session.commit()
    for s in (printer, red, rfid):
        await db_session.refresh(s)
    db_session.add_all(
        [
            SpoolAssignment(spool_id=red.id, printer_id=printer.id, ams_id=0, tray_id=0, fingerprint_type="PETG"),
            SpoolAssignment(spool_id=rfid.id, printer_id=printer.id, ams_id=0, tray_id=1, fingerprint_type="PETG"),
            SpoolAssignment(spool_id=red.id, printer_id=printer.id, ams_id=0, tray_id=2, fingerprint_type=""),
        ]
    )
    await db_session.commit()
    state = SimpleNamespace(
        nozzles=[],
        support_user_preset=False,
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        dict(MANUAL, id=0, tray_type="PETG", tray_color="FF0000FF", tray_info_idx="GFG99", cali_idx=3),
                        dict(
                            MANUAL,
                            id=1,
                            tray_type="PETG",
                            tray_color="0000FFFF",
                            tray_info_idx="GFG99",
                            tray_uuid="0123456789ABCDEF0123456789ABCDEF",
                        ),
                        {"id": 2},  # the slot is empty
                    ],
                }
            ]
        },
    )
    return printer, state


@pytest.mark.asyncio
async def test_dry_run_lists_apply_and_skip_rows_and_publishes_nothing(db_session):
    printer, state = await _farm(db_session)
    client = MagicMock()
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        result = await bulk.bulk_apply(db_session, printer, client, dry_run=True)
    by_slot = {(r["ams_id"], r["tray_id"]): r for r in result["rows"]}
    assert by_slot[(0, 0)]["action"] == "apply" and by_slot[(0, 0)]["advertised"]["tray_color"] == "000000FF"
    assert by_slot[(0, 1)]["action"] == "skip" and by_slot[(0, 1)]["reasons"] == ["rfid_slot_excluded"]
    assert by_slot[(0, 2)]["action"] == "skip" and "slot_empty" in by_slot[(0, 2)]["reasons"]
    assert result["dry_run"] is True and result["applied"] == 0 and result["would_apply"] == 1
    client.ams_set_filament_setting.assert_not_called()
    assert overlay.entries_for(printer.id) == {}


@pytest.mark.asyncio
async def test_apply_publishes_only_apply_rows_keeps_live_k_and_fills_the_overlay(db_session):
    printer, state = await _farm(db_session)
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        result = await bulk.bulk_apply(db_session, printer, client, dry_run=False)
    assert (result["applied"], result["skipped"]) == (1, 2)
    client.ams_set_filament_setting.assert_called_once()
    assert client.ams_set_filament_setting.call_args.kwargs["tray_color"] == "000000FF"
    client.extrusion_cali_sel.assert_called_once()
    assert client.extrusion_cali_sel.call_args.kwargs["cali_idx"] == 3
    assert client.extrusion_cali_sel.call_args.kwargs["nozzle_diameter"] == "0.4"
    # The walk already read the live state; the nozzle rides out on it rather
    # than being asked for a second time.
    assert pm.get_status.call_count == 1
    assert (0, 0) in overlay.entries_for(printer.id) and (0, 1) not in overlay.entries_for(printer.id)


@pytest.mark.asyncio
async def test_policy_off_reverts_a_slot_the_overlay_still_remembers(db_session):
    printer, state = await _farm(db_session)
    printer.ams_policies = {}  # policy switched off after the slot was advertised black
    await db_session.commit()
    from backend.app.services.ams_advertised_overlay import OverlayEntry

    overlay.replace_printer(
        printer.id, {(0, 0): OverlayEntry("PETG", "FF0000FF", "GFG99", (), "000000FF", "GFG99", "internal")}
    )
    state.raw_data["ams"][0]["tray"][0]["tray_color"] = "000000FF"  # the printer still shows black
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        result = await bulk.bulk_apply(db_session, printer, client, dry_run=False)
    row = next(r for r in result["rows"] if (r["ams_id"], r["tray_id"]) == (0, 0))
    assert row["action"] == "revert" and row["published"] is True
    assert client.ams_set_filament_setting.call_args.kwargs["tray_color"] == "FF0000FF"
    assert (0, 0) not in overlay.entries_for(printer.id)


@pytest.mark.asyncio
async def test_refresh_overlay_rebuilds_from_the_registry(db_session):
    printer, state = await _farm(db_session)
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        await bulk.refresh_overlay(db_session, printer)
    entries = overlay.entries_for(printer.id)
    # (0,2) is projected too: emptiness is a bulk question, not a projection one.
    assert set(entries) == {(0, 0), (0, 2)} and entries[(0, 0)].actual_color == "FF0000FF"


async def _spoolman_slot(db_session, printer, state, *, with_kprofile: bool) -> None:
    state.raw_data["ams"][0]["tray"].append(dict(MANUAL, id=3, tray_type="PETG", tray_color="00FF00FF"))
    # The walk covers the slots of the CURRENT inventory mode (spec §4.3), so a
    # Spoolman row is only this install's slot while Spoolman is the inventory.
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=3, spoolman_spool_id=SPOOLMAN_SPOOL))
    await db_session.commit()
    if not with_kprofile:
        return
    fc = FilamentCalibration(
        printer_id=printer.id,
        filament_id="GFG02",  # Bambu PETG HF — a BRANDED family, not the generic GFG99
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        cali_mode="pa",
        source="printer_sync",
        name="PETG HF",
    )
    db_session.add(fc)
    await db_session.commit()
    await db_session.refresh(fc)
    db_session.add(
        SpoolmanKProfile(
            spoolman_spool_id=SPOOLMAN_SPOOL, printer_id=printer.id, extruder=0, filament_calibration_id=fc.id
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_a_spoolman_slot_is_built_from_the_family_its_k_profile_links(db_session):
    """The two Spoolman assign routes key the slot off the linked calibration's
    family; a revert that published the generic instead would send a plan the
    slot never had."""
    printer, state = await _farm(db_session)
    await _spoolman_slot(db_session, printer, state, with_kprofile=True)
    stub = SimpleNamespace(get_spool=AsyncMock(return_value=SPOOLMAN_RAW))
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm,
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=stub)),
    ):
        pm.get_status.return_value = state
        walk = await bulk.iter_slot_projections(db_session, printer)
    candidate = next(c for c in walk.candidates if (c.ams_id, c.tray_id) == (0, 3))
    assert candidate.source == "spoolman" and walk.spoolman_deferred is False
    assert candidate.projection.actual.tray_info_idx == "GFG02"
    assert candidate.kprofile_filament_id == "GFG02"


@pytest.mark.asyncio
async def test_a_spoolman_slot_without_a_link_falls_back_to_the_generic(db_session):
    printer, state = await _farm(db_session)
    await _spoolman_slot(db_session, printer, state, with_kprofile=False)
    stub = SimpleNamespace(get_spool=AsyncMock(return_value=SPOOLMAN_RAW))
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm,
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=stub)),
    ):
        pm.get_status.return_value = state
        walk = await bulk.iter_slot_projections(db_session, printer)
    candidate = next(c for c in walk.candidates if (c.ams_id, c.tray_id) == (0, 3))
    assert candidate.projection.actual.tray_info_idx == "GFG99" and candidate.kprofile_filament_id == "GFG99"


@pytest.mark.asyncio
async def test_the_walk_says_when_the_spoolman_client_is_not_up_yet(db_session):
    printer, state = await _farm(db_session)
    await _spoolman_slot(db_session, printer, state, with_kprofile=False)
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm,
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=None)),
    ):
        pm.get_status.return_value = state
        walk = await bulk.iter_slot_projections(db_session, printer)
    assert walk.spoolman_deferred is True
    assert all(c.source == "internal" for c in walk.candidates)


@pytest.mark.asyncio
async def test_rebuild_once_walks_a_printer_once_per_process(db_session):
    printer, _ = await _farm(db_session)
    with patch.object(bulk, "refresh_overlay", new=AsyncMock(return_value=bulk.RebuildOutcome.COMPLETE)) as refresh:
        await bulk.rebuild_once(printer.id)
        await bulk.rebuild_once(printer.id)
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebuild_once_retries_while_the_spoolman_client_is_not_up(db_session):
    """A rebuild that silently dropped the whole Spoolman registry must not
    count as the one rebuild this process gets."""
    printer, _ = await _farm(db_session)
    with patch.object(bulk, "refresh_overlay", new=AsyncMock(return_value=bulk.RebuildOutcome.DEFERRED)) as refresh:
        await bulk.rebuild_once(printer.id)
        await bulk.rebuild_once(printer.id)
    assert refresh.await_count == 2


@pytest.mark.asyncio
async def test_rebuild_once_ignores_a_printer_that_is_gone(db_session):
    with patch.object(bulk, "refresh_overlay", new=AsyncMock()) as refresh:
        await bulk.rebuild_once(999999)
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_spoolman_spool_defers_the_walk_too(db_session):
    """ "Spoolman is down" and "the client is not up yet" are the same answer.

    A per-spool ``continue`` left the walk looking COMPLETE, and the rebuild
    then replaced a correct overlay with one missing every Spoolman slot."""
    printer, state = await _farm(db_session)
    await _spoolman_slot(db_session, printer, state, with_kprofile=False)
    stub = SimpleNamespace(get_spool=AsyncMock(side_effect=RuntimeError("connection refused")))
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm,
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=stub)),
    ):
        pm.get_status.return_value = state
        walk = await bulk.iter_slot_projections(db_session, printer)
    assert walk.spoolman_deferred is True
    assert all(c.source == "internal" for c in walk.candidates)


@pytest.mark.asyncio
async def test_internal_mode_reads_no_spoolman_rows_and_never_defers(db_session):
    """Leftover rows of a mode that was switched off are absent, not deferred —
    the Spoolman client is never coming up, and the retry rides a hot callback."""
    printer, state = await _farm(db_session)
    await _spoolman_slot(db_session, printer, state, with_kprofile=False)
    await db_session.execute(delete(Settings).where(Settings.key == "spoolman_enabled"))
    await db_session.commit()
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm,
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=None)) as client,
    ):
        pm.get_status.return_value = state
        walk = await bulk.iter_slot_projections(db_session, printer)
    assert walk.spoolman_deferred is False
    assert all(c.source == "internal" for c in walk.candidates)
    client.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_setting_defers_instead_of_guessing_internal_mode(db_session):
    """A settings read that FAILED is not "this install is not Spoolman".

    Answering False there silently drops the whole Spoolman half and still
    reports COMPLETE, so the rebuild replaces a correct overlay with a halved
    one and never asks again."""
    printer, state = await _farm(db_session)
    await _spoolman_slot(db_session, printer, state, with_kprofile=False)
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm,
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(side_effect=RuntimeError("db gone"))),
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=None)) as client,
    ):
        pm.get_status.return_value = state
        walk = await bulk.iter_slot_projections(db_session, printer)
    assert walk.spoolman_deferred is True
    assert all(c.source == "internal" for c in walk.candidates)
    client.assert_not_awaited()  # unknown mode reads no Spoolman row at all


@pytest.mark.asyncio
async def test_a_deferred_walk_leaves_the_existing_entries_alone(db_session):
    """A half answer may not replace the map: the entries the assign routes
    wrote are EXACT, and every Spoolman slot would be dropped from them."""
    printer, state = await _farm(db_session)
    await _spoolman_slot(db_session, printer, state, with_kprofile=False)
    from backend.app.services.ams_advertised_overlay import OverlayEntry

    exact = {(0, 3): OverlayEntry("PETG", "00FF00FF", "GFG99", (), "000000FF", "GFG99", "spoolman")}
    overlay.replace_printer(printer.id, exact)
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm,
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=None)),
    ):
        pm.get_status.return_value = state
        outcome = await bulk.refresh_overlay(db_session, printer)
    assert outcome is bulk.RebuildOutcome.DEFERRED
    assert overlay.entries_for(printer.id) == exact


@pytest.mark.asyncio
async def test_a_walk_that_threw_is_not_a_rebuild(db_session):
    """ "Complete" and "threw" answered the same before — so one exception in a
    hot callback spent the printer's one rebuild and left the overlay empty."""
    printer, _ = await _farm(db_session)
    with patch.object(bulk, "iter_slot_projections", new=AsyncMock(side_effect=RuntimeError("boom"))):
        outcome = await bulk.refresh_overlay(None, printer)
    assert outcome is bulk.RebuildOutcome.FAILED
    assert overlay.entries_for(printer.id) == {}
    with patch.object(bulk, "refresh_overlay", new=AsyncMock(return_value=bulk.RebuildOutcome.FAILED)) as refresh:
        await bulk.rebuild_once(printer.id)
        await bulk.rebuild_once(printer.id)
    assert refresh.await_count == 2  # not marked done, asked again on the next push


@pytest.mark.asyncio
async def test_a_rebuild_gives_up_after_five_deferred_attempts(db_session, caplog):
    """The retry rides ``on_ams_change``, which fires several times a minute per
    printer. A Spoolman that never comes back must not buy an unbounded walk."""
    printer, _ = await _farm(db_session)
    with (
        patch.object(bulk, "refresh_overlay", new=AsyncMock(return_value=bulk.RebuildOutcome.DEFERRED)) as refresh,
        caplog.at_level("WARNING"),
    ):
        for _ in range(bulk.MAX_DEFERRED_ATTEMPTS + 3):
            await bulk.rebuild_once(printer.id)
    assert refresh.await_count == bulk.MAX_DEFERRED_ATTEMPTS
    assert str(printer.id) in caplog.text


@pytest.mark.asyncio
async def test_bulk_apply_says_when_the_list_is_only_half_the_farm(db_session):
    printer, state = await _farm(db_session)
    await _spoolman_slot(db_session, printer, state, with_kprofile=False)
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm,
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=None)),
    ):
        pm.get_status.return_value = state
        preview = await bulk.bulk_apply(db_session, printer, MagicMock(), dry_run=True)
    assert preview["spoolman_unavailable"] is True
    assert all(r["source"] == "internal" for r in preview["rows"])


async def _policy_switched_off(db_session, live_color: str):
    printer, state = await _farm(db_session)
    printer.ams_policies = {}
    await db_session.commit()
    state.raw_data["ams"][0]["tray"][0]["tray_color"] = live_color
    return printer, state


@pytest.mark.asyncio
async def test_a_restart_with_the_policy_off_recovers_what_we_advertised(db_session):
    """Otherwise a restart STRANDS every masked slot: the walk projects
    ``policy_off`` for all of them, the overlay is emptied, routing believes the
    masked live values and the bulk button never offers the revert that would
    undo them — the section renders off the very entries it just lost."""
    printer, state = await _policy_switched_off(db_session, "000000FF")
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        assert await bulk.refresh_overlay(db_session, printer) is bulk.RebuildOutcome.COMPLETE
    entry = overlay.entries_for(printer.id)[(0, 0)]
    assert (entry.actual_color, entry.advertised_color) == ("FF0000FF", "000000FF")
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        preview = await bulk.bulk_apply(db_session, printer, MagicMock(), dry_run=True)
    row = next(r for r in preview["rows"] if (r["ams_id"], r["tray_id"]) == (0, 0))
    assert row["action"] == "revert" and row["advertised"]["tray_color"] == "FF0000FF"


@pytest.mark.asyncio
async def test_recovery_re_projects_the_STORED_canonical_colour_not_the_default(db_session):
    """The switch is off but the colour it was last set to is still on the row.

    Re-projecting the DEFAULT black instead would recover exactly the installs
    that never changed the colour and strand every other one — and would adopt
    a black tray nobody here ever advertised."""
    printer, state = await _farm(db_session)
    printer.ams_policies = {
        "backup_compatibility": {
            "normalize_color": False,
            "canonical_color_rgba": "1A2B3CFF",
            "generic_base_material": False,
        }
    }
    await db_session.commit()
    state.raw_data["ams"][0]["tray"][0]["tray_color"] = "1A2B3CFF"
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        assert await bulk.refresh_overlay(db_session, printer) is bulk.RebuildOutcome.COMPLETE
    entry = overlay.entries_for(printer.id)[(0, 0)]
    assert (entry.actual_color, entry.advertised_color) == ("FF0000FF", "1A2B3CFF")

    # The default colour is not this printer's colour: a black tray here was
    # somebody else's doing.
    state.raw_data["ams"][0]["tray"][0]["tray_color"] = "000000FF"
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        await bulk.refresh_overlay(db_session, printer)
    assert overlay.entries_for(printer.id) == {}


@pytest.mark.asyncio
async def test_a_live_colour_we_could_never_have_advertised_is_not_ours(db_session):
    """Somebody set this slot from the printer's screen. No entry — the live
    tray stays the truth and the existing auto-unlink rule decides its fate."""
    printer, state = await _policy_switched_off(db_session, "123456FF")
    with patch("backend.app.services.ams_backup_compatibility_apply.printer_manager") as pm:
        pm.get_status.return_value = state
        await bulk.refresh_overlay(db_session, printer)
        preview = await bulk.bulk_apply(db_session, printer, MagicMock(), dry_run=True)
    assert overlay.entries_for(printer.id) == {}
    row = next(r for r in preview["rows"] if (r["ams_id"], r["tray_id"]) == (0, 0))
    assert row["action"] == "skip" and row["reasons"] == ["policy_off"]
