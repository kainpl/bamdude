"""Add ``nozzle_mapping`` to ``print_queue``.

H2C dual-nozzle-rack slicer-pick preservation (#1780). BambuStudio's
project_file MQTT command for rack-swap-capable models (O1C2 today) carries the
slicer's per-filament physical nozzle position IDs — a ``list[int]`` straight
off the wire. The Virtual Printer intake now stashes it verbatim as an opaque
JSON string on the queue item; the dispatcher replays it into
``command["print"]["nozzle_mapping"]`` on dual-nozzle machines so the firmware
honours the user's slicer pick instead of falling back to "last matching nozzle
type" auto-pick. NULL on every other model.

Nullable TEXT — no Postgres / SQLite divergence. Fresh installs get the column
from the model's ``create_all``; this backfills existing DBs.

Distinct from ``utils/threemf_tools.extract_nozzle_mapping_from_3mf`` (a
server-derived slot→physical-extruder ``dict[int, int]`` that feeds per-filament
``nozzle_id`` and never reaches ``command["print"]``).

Upstream Bambuddy #1780 / commit ``d196cfc5`` (which used an inline
``run_migrations`` ALTER; BamDude uses numbered migrations). ``nozzles_info``
from the original upstream fix is intentionally NOT ported — BambuStudio never
actually sends it (confirmed via wire capture).
"""

from backend.app.migrations.helpers import add_column

version = 98
name = "print_queue_nozzle_mapping"


async def upgrade(conn):
    await add_column(conn, "print_queue", "nozzle_mapping TEXT")
