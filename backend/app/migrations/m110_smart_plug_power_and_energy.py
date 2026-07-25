"""Three ``smart_plugs`` columns: accessory-plug flag + REST lifetime-energy source.

**``controls_printer_power``** (upstream #2629). Every plug linked to a printer was
treated as that printer's power source, so switching off an *accessory* plug — a
chamber filter, an enclosure light, a filament dryer — marked the printer offline
and blanked its state to ``"unknown"``. In BamDude that is worse than upstream: a
stuck ``"unknown"`` blocks BOTH queue tiers, because ``print_scheduler`` only
dispatches on IDLE/FINISH/FAILED and the AutoQueue distributor asks the same
question. Defaults to TRUE so every existing plug keeps today's behaviour; the
user turns it off on the plugs that aren't the printer's mains feed.

**``rest_energy_total_path`` / ``rest_energy_total_multiplier``** (upstream #2539).
``RESTSmartPlugService.get_energy`` filed whatever a REST endpoint returned under
``energy["today"]`` and never set ``total``, so a REST plug reporting a lifetime
counter was mislabelled as a daily figure. Consequences in our tree, all of which
these columns fix: ``smart_plug_manager._capture_energy_snapshots`` skips any plug
whose ``total`` is None, so a REST plug never produced a single
``smart_plug_energy_snapshots`` row and every date-range energy figure was blank
for it; and per-print energy was dead at both ends, because
``_record_energy_start`` and the completion handler both bail when ``total`` is
None — so ``PrintArchive.energy_kwh`` / ``energy_cost`` were never written for a
REST plug either. The lifetime source is configured separately from the "today"
one because a given endpoint may expose either, both, or neither.

All three are additive and nullable-or-defaulted, so existing rows need no
backfill. Fresh installs get them from the model's ``create_all``; this adds them
to existing DBs. Idempotent (``add_column`` no-ops when the column exists, incl.
the DEBUG-startup re-run).
"""

from backend.app.migrations.helpers import add_column

version = 110
name = "smart_plug_power_and_energy"


async def upgrade(conn):
    # DEFAULT 1: an existing linked plug is presumed to be the printer's power
    # source, which is what the code did unconditionally before this column.
    await add_column(conn, "smart_plugs", "controls_printer_power BOOLEAN DEFAULT 1 NOT NULL")
    await add_column(conn, "smart_plugs", "rest_energy_total_path VARCHAR(200)")
    await add_column(conn, "smart_plugs", "rest_energy_total_multiplier FLOAT DEFAULT 1.0 NOT NULL")
