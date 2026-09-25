"""A tray's material → the row that answers for it in a table keyed by base material.

The printer reports filled and foamed variants in full (PLA-CF, PETG-CF, ABS-GF,
PLA-AERO, PA6-CF) while the drying presets, the per-filament humidity thresholds
and the preheat chamber targets are keyed by base material. Matching the raw
string found a row for 8 of the 41 types a printer can report, and every caller
reads "no row" as "nothing to do", so auto-drying silently skipped every
composite spool (upstream #3067). Leaf module — nothing here imports the app.
"""

from collections.abc import Mapping

# Materials whose AMS spelling differs from the table key. Bambu labels nylon
# "PA" while its own composites spell the family out. PPA (polyphthalamide) is a
# distinct polymer, not a nylon grade — but an aromatic polyamide that takes up
# moisture the same way, and PA's row is the hottest the table has. Mirrors
# DRYING_MATERIAL_ALIASES in frontend/src/utils/dryingPresets.ts (a test pins it).
MATERIAL_KEY_ALIASES: dict[str, str] = {
    "NYLON": "PA",
    "PA6": "PA",
    "PA11": "PA",
    "PA12": "PA",
    "PAHT": "PA",
    "PPA": "PA",
}


def resolve_material_key(tray_type: str | None, table: Mapping[str, object]) -> str | None:
    """The key in ``table`` that answers for this material, or ``None``.

    Exact first (a row the user added for ``PA6-CF`` still wins), then the base
    without its suffix, each through the alias map. ``None`` rather than a default:
    only the caller knows whether "no row" means skip or fall back.
    """
    text = (tray_type or "").strip()
    if not text:
        return None
    raw = text.split()[0].upper()
    for candidate in (raw, raw.split("-")[0]):
        key = MATERIAL_KEY_ALIASES.get(candidate, candidate)
        if key in table:
            return key
    return None
