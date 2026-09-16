"""In-memory record of every AMS slot we advertised differently from its spool.

Routing (``printer_feed_snapshot``), the dispatcher's loaded list and the
status payload ask this store for the ACTUAL spool behind an advertised
profile (spec §6.3). It is memory only, on purpose: ``get_feed_snapshot`` runs
synchronously under the client's routing lock and may not touch the database.
Process restart rebuilds it from both assignment registries
(``ams_backup_compatibility_apply.refresh_overlay``); a reconnect keeps it —
the store is keyed by printer, not by MQTT client.

An entry is EFFECTIVE only while the live tray still equals what we advertised
(``matches_live``): before the printer echoes our push it stays silent, and a
slot somebody reconfigured from the printer screen is never overlaid. It is
dormant then, not deleted — the echo may still be on its way — and it leaves
only when the assignment does (``forget``) or the map is rebuilt.

⚠️ This module imports NOTHING from ``backend.app.services`` (hence its own
``_norm_color``): ``printer_feed_snapshot`` imports ``matches_live`` at module
level, and any service import here would close that cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OverlayEntry:
    actual_material: str
    actual_color: str
    actual_variant: str
    actual_cols: tuple[str, ...]
    advertised_color: str
    advertised_variant: str
    source: str  # "internal" | "spoolman"


# {printer_id: {(ams_id, tray_id): OverlayEntry}}
_store: dict[int, dict[tuple[int, int], OverlayEntry]] = {}


def _norm_color(value) -> str:
    """Six hex digits, upper — alpha and '#' are not identity."""
    return str(value or "").strip().lstrip("#").upper()[:6]


def entry_from(projection, source: str) -> OverlayEntry:
    a, v = projection.actual, projection.advertised
    return OverlayEntry(
        actual_material=a.tray_type,
        actual_color=a.tray_color,
        actual_variant=a.tray_info_idx,
        actual_cols=tuple(a.cols or ()),
        advertised_color=v.tray_color,
        advertised_variant=v.tray_info_idx,
        source=source,
    )


def remember(printer_id: int, ams_id: int, tray_id: int, projection, source: str) -> None:
    if not projection.projected:
        forget(printer_id, ams_id, tray_id)
        return
    _store.setdefault(printer_id, {})[(ams_id, tray_id)] = entry_from(projection, source)


def forget(printer_id: int, ams_id: int, tray_id: int) -> None:
    slots = _store.get(printer_id)
    if slots:
        slots.pop((ams_id, tray_id), None)
        if not slots:
            _store.pop(printer_id, None)


def forget_printer(printer_id: int) -> None:
    _store.pop(printer_id, None)


def forget_all() -> None:
    _store.clear()


def replace_printer(printer_id: int, entries: dict[tuple[int, int], OverlayEntry]) -> None:
    if entries:
        _store[printer_id] = dict(entries)
    else:
        _store.pop(printer_id, None)


def entries_for(printer_id: int) -> dict[tuple[int, int], OverlayEntry]:
    return dict(_store.get(printer_id, {}))


def matches_live(entry: OverlayEntry, live_tray: dict | None) -> bool:
    if not live_tray:
        return False
    idx = str(live_tray.get("tray_info_idx") or "").strip().upper()
    return idx == entry.advertised_variant.strip().upper() and _norm_color(live_tray.get("tray_color")) == _norm_color(
        entry.advertised_color
    )


def effective(printer_id: int, ams_id: int, tray_id: int, live_tray: dict | None) -> OverlayEntry | None:
    entry = _store.get(printer_id, {}).get((ams_id, tray_id))
    if entry is None or not matches_live(entry, live_tray):
        return None
    return entry
