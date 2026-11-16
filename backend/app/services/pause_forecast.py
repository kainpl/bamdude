"""The next pause the slicer put into the running print; a read-only forecast.

The pauses are a fact of the print's own unpatched file
(``PrintFileAnalysis.pause_points``). Whether one lies AHEAD is asked only of
the proven observed run: the layer is the completed MQTT snapshot's, addressed
to the same run as the layer grant -- never the raw ``state.layer_num`` (a stale
frame of the previous print, an unproven file change and a catch-up all pass
through it), and nothing here comes from a command acknowledgement.

Unknown is absent, never a guess: no client, no snapshot, no grant, another
address, an unproven snapshot, any state but RUNNING, a disconnected printer, an
unknown layer total, or a file that is not analysed (or has no comparable
layers) all answer ``None``.

No I/O, no writes: nothing here touches ``PrinterState``, the queue, the
archive or the printer.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.services.layer_projection import current_layer_grant, read_layer_snapshot, resolve_layer_pair
from backend.app.services.layer_source import positive_integer
from backend.app.services.print_file_analysis import current_pause_points

# The slicer's times are for Standard speed; in another mode the firmware
# rescales its own remaining time by a factor nobody measured.
STANDARD_SPEED_LEVEL = 2


@dataclass(frozen=True)
class PauseForecast:
    """One upcoming pause of one run. ``archive_identity`` + ``layer`` name it once."""

    printer_id: int
    archive_id: int
    archive_identity: str
    layer: int
    minutes: int | None
    layer_now: int
    total_layers: int


def forecast_next_pause(manager, printer_id: int | None) -> PauseForecast | None:
    if printer_id is None:
        return None
    clients = getattr(manager, "_clients", None)
    client = clients.get(printer_id) if isinstance(clients, dict) else None
    if client is None:
        return None
    snapshot = read_layer_snapshot(manager, printer_id)
    if snapshot is None or snapshot.unproven or snapshot.state != "RUNNING":
        return None
    grant = current_layer_grant(manager, printer_id)
    state = client.state
    if grant is None or snapshot.address != grant.address or not state.connected:
        return None
    total = resolve_layer_pair(manager, printer_id, snapshot=snapshot).total_layers
    if not positive_integer(total):
        return None
    points = current_pause_points(manager, printer_id, grant.archive_id, grant.archive_identity)
    if not points:
        return None
    layer_now = snapshot.layer_num
    # Strictly above: the printer stands on a pause's layer while it waits there
    # and after it resumes, so that pause is behind it.
    point = next((point for point in points if layer_now < point[0] <= total), None)
    if point is None:
        return None
    layer, slicer_minutes = point
    remaining = getattr(state, "remaining_time", None)
    minutes = None
    if (
        slicer_minutes is not None
        and getattr(state, "speed_level", None) == STANDARD_SPEED_LEVEL
        and type(remaining) is int
        and remaining > 0
    ):
        # Both count down to the end of the same print; the difference is the time to the pause.
        minutes = max(0, remaining - slicer_minutes)
    return PauseForecast(printer_id, grant.archive_id, grant.archive_identity, layer, minutes, layer_now, total)


def next_pause(manager, printer_id: int | None) -> dict | None:
    """The status field: ``{"layer", "minutes"}`` or ``None``."""
    forecast = forecast_next_pause(manager, printer_id)
    return None if forecast is None else {"layer": forecast.layer, "minutes": forecast.minutes}
