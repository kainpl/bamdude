"""An addressed display projection; never writes MQTT counters or print effects.

Grants live on the application loop. MQTT publishes immutable snapshots under
its own short lock. No reader performs database, filesystem or network I/O.
"""

import asyncio
from dataclasses import dataclass, replace
from typing import Literal

from backend.app.services.layer_source import SOURCE_FIELDS, positive_integer, select_layer_total
from backend.app.services.print_observation import PrintObservation
from backend.app.services.print_run_binding import PrintEffectToken, current_print_run, print_effect_is_current


@dataclass(frozen=True)
class LayerAddress:
    lineage: str
    generation: int
    revision: int


@dataclass(frozen=True)
class LayerSnapshot:
    address: LayerAddress
    state: str
    layer_num: int
    total_layers: int
    unproven: bool


@dataclass(frozen=True)
class LayerPair:
    layer_num: int = 0
    total_layers: int = 0
    source: Literal["mqtt", "file", "unknown"] = "unknown"


@dataclass(frozen=True)
class LayerGrant:
    printer_id: int
    printer_life: int
    archive_id: int
    archive_identity: str
    effect_generation: int
    address: LayerAddress
    plate_index: int | None
    source_stamp: tuple
    source_revision: int
    total: int | None


def _grants(manager) -> dict:
    registry = getattr(manager, "_layer_grants", None)
    if not isinstance(registry, dict):
        registry = {}
        manager._layer_grants = registry
    return registry


def observation_address(observation) -> LayerAddress | None:
    if (
        isinstance(observation, PrintObservation)
        and observation.lineage
        and type(observation.layer_identity_revision) is int
    ):
        return LayerAddress(observation.lineage, observation.generation, observation.layer_identity_revision)
    return None


def request_layer_publication(manager, printer_id: int) -> None:
    """Coalesce changes on the app loop; no tasks/polling for metadata refresh."""
    callback = getattr(manager, "_layer_projection_callback", None)
    if not callable(callback):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    pending = getattr(manager, "_layer_publications_pending", None)
    if not isinstance(pending, set):
        pending = set()
        manager._layer_publications_pending = pending
    if printer_id in pending:
        return
    pending.add(printer_id)

    def publish():
        pending.discard(printer_id)
        callback(printer_id)

    loop.call_soon(publish)


def clear_layer_grant(manager, printer_id: int) -> None:
    if _grants(manager).pop(printer_id, None) is not None:
        request_layer_publication(manager, printer_id)


def current_layer_grant(manager, printer_id: int) -> LayerGrant | None:
    grant = _grants(manager).get(printer_id)
    run = current_print_run(manager, printer_id)
    if (
        grant is not None
        and run is not None
        and run.archive_id == grant.archive_id
        and run.archive_identity == grant.archive_identity
        and run.effect_generation == grant.effect_generation
        and manager.printer_life_is_current(printer_id, grant.printer_life)
        and print_effect_is_current(manager, PrintEffectToken(printer_id, grant.archive_id, grant.effect_generation))
    ):
        return grant
    return None


def _source(archive) -> tuple[tuple, int | None]:
    plate = getattr(archive, "plate_index", None)
    total = select_layer_total(archive, plate)
    return ((*[getattr(archive, key, None) for key in SOURCE_FIELDS], plate, total), total)


def bind_layer_grant(manager, printer_id, archive, *, expected_generation, origin, observation) -> None:
    """Called after a successful bind. Authority is the caller's proof, not run.origin."""
    previous = _grants(manager).get(printer_id)
    identity = getattr(archive, "recovery_identity", None)
    if previous is not None and (previous.archive_id != archive.id or previous.archive_identity != identity):
        clear_layer_grant(manager, printer_id)
        previous = None
    address = observation_address(observation)
    run = current_print_run(manager, printer_id)
    if (
        expected_generation is None
        or origin not in {"dispatch", "external", "exact_subtask"}
        or address is None
        or not identity
        or run is None
        or run.archive_id != archive.id
        or run.archive_identity != identity
        or run.effect_generation != expected_generation
        or not print_effect_is_current(manager, PrintEffectToken(printer_id, archive.id, expected_generation))
    ):
        return
    stamp, total = _source(archive)
    grant = LayerGrant(
        printer_id,
        manager.printer_life(printer_id),
        archive.id,
        identity,
        expected_generation,
        address,
        archive.plate_index,
        stamp,
        previous.source_revision
        if previous and previous.source_stamp == stamp
        else (previous.source_revision + 1 if previous else 1),
        total,
    )
    if previous != grant:
        _grants(manager)[printer_id] = grant
        request_layer_publication(manager, printer_id)


def notify_current_print_source_ready(manager, printer_id: int, archive) -> None:
    """All attach paths use the committed row. Source readiness grants no authority."""
    from backend.app.core.config import settings
    from backend.app.services.print_file_analysis import notify_print_file_analysis_source_ready

    run = current_print_run(manager, printer_id)
    identity = getattr(archive, "recovery_identity", None)
    if run is None or run.archive_id != archive.id or run.archive_identity != identity:
        return
    grant = current_layer_grant(manager, printer_id)
    if grant is not None:
        stamp, total = _source(archive)
        if stamp != grant.source_stamp:
            _grants(manager)[printer_id] = replace(
                grant,
                source_stamp=stamp,
                source_revision=grant.source_revision + 1,
                plate_index=archive.plate_index,
                total=total,
            )
            request_layer_publication(manager, printer_id)
    if archive.file_path:
        notify_print_file_analysis_source_ready(
            manager,
            printer_id,
            archive.id,
            settings.base_dir / archive.file_path,
            archive.plate_index,
            expected_identity=identity,
        )


def _client(manager, printer_id):
    clients = getattr(manager, "_clients", None)
    return clients.get(printer_id) if isinstance(clients, dict) else None


def read_layer_snapshot(manager, printer_id: int) -> LayerSnapshot | None:
    client = _client(manager, printer_id)
    if client is None:
        return None
    snapshot = client.get_layer_snapshot()
    return snapshot if isinstance(snapshot, LayerSnapshot) else None


_SNAPSHOT_UNSET = object()


def resolve_layer_pair(manager, printer_id: int | None, state=None, *, snapshot=_SNAPSHOT_UNSET) -> LayerPair:
    if snapshot is _SNAPSHOT_UNSET:
        snapshot = read_layer_snapshot(manager, printer_id) if printer_id is not None else None
    client = _client(manager, printer_id)
    # Legacy state-only callers can still serialize raw telemetry. A real
    # client must publish its first completed snapshot before any file grant.
    if snapshot is None:
        if client is not None:
            return LayerPair()
        layer = getattr(state, "layer_num", 0) or 0
        total = getattr(state, "total_layers", 0) or 0
        return LayerPair(layer, total, "mqtt") if positive_integer(total) else LayerPair(layer)
    if positive_integer(snapshot.total_layers):
        return LayerPair(snapshot.layer_num, snapshot.total_layers, "mqtt")
    grant = current_layer_grant(manager, printer_id)
    if (
        grant is not None
        and snapshot.address == grant.address
        and not snapshot.unproven
        and snapshot.state in {"RUNNING", "PAUSE"}
        and client.state.connected
        and positive_integer(grant.total)
        and grant.total >= snapshot.layer_num
    ):
        return LayerPair(snapshot.layer_num, grant.total, "file")
    return LayerPair(snapshot.layer_num)


def capture_first_layer_total(manager, printer_id, observation):
    snapshot = read_layer_snapshot(manager, printer_id)
    pair = resolve_layer_pair(manager, printer_id, snapshot=snapshot)
    if snapshot is not None and snapshot.address == observation_address(observation) and pair.source == "file":
        return current_layer_grant(manager, printer_id), snapshot.address, pair.total_layers
    return None


def first_layer_total_is_current(manager, printer_id, captured) -> bool:
    if captured is None:
        return False
    grant, address, total = captured
    snapshot = read_layer_snapshot(manager, printer_id)
    pair = resolve_layer_pair(manager, printer_id, snapshot=snapshot)
    return (
        current_layer_grant(manager, printer_id) == grant
        and snapshot is not None
        and snapshot.address == address
        and pair.source == "file"
        and pair.total_layers == total
    )
