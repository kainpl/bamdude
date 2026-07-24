"""Start-time built-in-inventory spool colour on PrintArchive.filament_color.

At print start ``apply_loaded_spool_colors`` prefers the loaded built-in spool's
colour per slot, else that slot's 3MF colour. No-op in Spoolman mode.
"""

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.archive_colors import apply_loaded_spool_colors

# slot 1 -> global tray 0 (ams0/tray0); slot 2 -> global tray 5 (ams1/tray1)
AMS_MAPPING = [0, 5]

PLATES = [
    {
        "index": 1,
        "filaments": [
            {"slot_id": 1, "color": "#161616", "used_grams": 10},
            {"slot_id": 2, "color": "#0011FF", "used_grams": 5},
        ],
    }
]


async def _make_archive(db, printer_id, *, plates, filament_color, plate_index=1):
    archive = PrintArchive(
        printer_id=printer_id,
        filename="x.gcode.3mf",
        file_path="archive/x",
        file_size=1,
        filament_color=filament_color,
        plate_index=plate_index,
        extra_data={"plates": plates} if plates is not None else {},
        status="printing",
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


@pytest.mark.asyncio
async def test_start_uses_builtin_spool_colour_per_slot(db_session, printer_factory):
    printer = await printer_factory(name="P1", serial_number="COLOR1")
    spool = Spool(material="PLA", rgba="000000FF")  # black
    db_session.add(spool)
    await db_session.flush()
    # Only slot 1's tray (ams0/tray0) has an assigned spool; slot 2 stays 3MF.
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
    await db_session.commit()

    archive = await _make_archive(db_session, printer.id, plates=PLATES, filament_color="#161616,#0011FF")
    await apply_loaded_spool_colors(db_session, archive, printer.id, AMS_MAPPING)
    await db_session.commit()
    await db_session.refresh(archive)

    assert archive.filament_color == "#000000,#0011FF"


@pytest.mark.asyncio
async def test_start_all_slots_from_spools(db_session, printer_factory):
    printer = await printer_factory(name="P1b", serial_number="COLOR1B")
    black = Spool(material="PLA", rgba="000000FF")
    green = Spool(material="PLA", rgba="00FF00FF")
    db_session.add_all([black, green])
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=black.id, printer_id=printer.id, ams_id=0, tray_id=0))
    db_session.add(SpoolAssignment(spool_id=green.id, printer_id=printer.id, ams_id=1, tray_id=1))
    await db_session.commit()

    archive = await _make_archive(db_session, printer.id, plates=PLATES, filament_color="#161616,#0011FF")
    await apply_loaded_spool_colors(db_session, archive, printer.id, AMS_MAPPING)
    await db_session.commit()
    await db_session.refresh(archive)

    assert archive.filament_color == "#000000,#00FF00"


@pytest.mark.asyncio
async def test_start_noop_in_spoolman_mode(db_session, printer_factory):
    printer = await printer_factory(name="P2", serial_number="COLOR2")
    spool = Spool(material="PLA", rgba="000000FF")
    db_session.add(spool)
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=0))
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    await db_session.commit()

    archive = await _make_archive(db_session, printer.id, plates=PLATES, filament_color="#161616,#0011FF")
    await apply_loaded_spool_colors(db_session, archive, printer.id, AMS_MAPPING)
    await db_session.commit()
    await db_session.refresh(archive)

    assert archive.filament_color == "#161616,#0011FF"  # Spoolman applies at completion instead
