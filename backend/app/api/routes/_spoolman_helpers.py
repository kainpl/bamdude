"""Pure helper functions for Spoolman spool mapping.

No heavy dependencies — importable in unit tests without the full backend stack.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from typing_extensions import TypedDict

from backend.app.api.routes._url_safety import assert_safe_lan_service_url
from backend.app.services.spoolman import native_tag_uids
from backend.app.services.spoolman_weights import spool_net_weight, spool_price_per_gram

logger = logging.getLogger(__name__)


class MappedSpoolFields(TypedDict):
    """Full shape of the dict returned by _map_spoolman_spool (InventorySpool-compatible)."""

    id: int
    material: str | None
    subtype: str | None
    brand: str | None
    color_name: str | None
    # True iff ``color_name`` was synthesised from ``subtype`` (no real
    # value stored in ``spool.extra.bambu_color_name`` or
    # ``filament.color_name``). Surfaced so the edit form can avoid
    # round-tripping the synth value back as if it were a real user
    # edit — upstream Bambuddy #1319 / #1357.
    color_name_is_synthesized: bool
    rgba: str | None
    extra_colors: str | None
    effect_type: None
    label_weight: int | None
    core_weight: int | None
    core_weight_catalog_id: None
    weight_used: float | None
    weight_used_baseline: float | None
    weight_locked: bool
    last_scale_weight: None
    last_weighed_at: None
    slicer_filament: None
    slicer_filament_name: str | None
    nozzle_temp_min: int | None
    nozzle_temp_max: None
    note: str | None
    added_full: None
    last_used: str | None
    # Last drying, stored under the bambu_last_dried_* extra keys.
    last_dried_at: str | None
    last_dried_temp: int | None
    last_dried_hours: float | None
    encode_time: str | None
    tag_uid: str | None
    tray_uuid: str | None
    data_origin: str | None
    tag_type: str | None
    archived_at: str | None
    created_at: str | None
    updated_at: str | None
    cost_per_kg: float | None
    # Read-only: the per-kg rate a print off this spool is charged — the
    # spool's own price, else its filament's. The form never reads it, so
    # saving a spool cannot copy the catalogue price onto it.
    effective_cost_per_kg: float | None
    storage_location: str | None
    location_id: int | None
    k_profiles: list[Any]


class NormalizedVendorRef(TypedDict):
    """Vendor reference embedded in a NormalizedFilament."""

    id: int
    name: str


class NormalizedFilament(TypedDict):
    """Normalised Spoolman filament dict returned by the /filaments catalog endpoint."""

    id: int
    name: str
    material: str | None
    color_hex: str | None
    color_name: str | None
    weight: int | None
    spool_weight: float | None
    vendor: NormalizedVendorRef | None


def assert_safe_spoolman_url(url: str) -> None:
    """Raise ``ValueError`` if *url* should be blocked as an SSRF risk.

    Spoolman is one of several services that legitimately live on the LAN, so
    this is the shared LAN-service policy with Spoolman's label — see
    :func:`_url_safety.assert_safe_lan_service_url` for what it accepts and why.

    It used to be a full copy of that logic, down to its own cloud-metadata set
    and its own inline numeric-IP regex. Kept as a named alias rather than
    deleted so the call sites and their tests keep reading in Spoolman's terms.
    """
    assert_safe_lan_service_url(url, label="Spoolman URL")


# Six characters, or eight when the filament carries an alpha byte. The write
# side stores eight only for genuinely translucent spools (upstream 73912d4f,
# #2912); rejecting them here turned every clear spool into neutral grey on read.
_COLOR_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")
_TAG_HEX_RE = re.compile(r"^[0-9A-F]+$")


def _safe_int(value: object, fallback: int) -> int:
    try:
        f = float(value)  # type: ignore[arg-type]
        if math.isfinite(f):
            return int(f)
    except (TypeError, ValueError):
        pass
    return fallback


def _safe_float(value: object, fallback: float) -> float:
    try:
        f = float(value)  # type: ignore[arg-type]
        if math.isfinite(f):
            return f
    except (TypeError, ValueError):
        pass
    return fallback


def _safe_optional_float(value: object) -> float | None:
    """Convert value to finite float, or None if missing/NaN/Infinite/non-numeric."""
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        if math.isfinite(f):
            return f
    except (TypeError, ValueError):
        pass
    return None


# Used only when Spoolman has no empty-spool weight at any level.
SPOOLMAN_FALLBACK_TARE = 250.0


def spoolman_tare(spool: dict) -> float:
    """The empty-spool weight of a Spoolman spool, in grams.

    Spoolman's own order: the spool's ``spool_weight``, then its filament's,
    then the vendor's ``empty_spool_weight``. Skipping the vendor made a spool
    whose tare lives only there weigh against 250 g (upstream #3195). 0 is a
    real tare; a value that is not a finite number counts as missing. Every
    Spoolman-mode tare goes through here, so the weigh endpoint and the core
    weight the form weighs with cannot drift apart.
    """
    filament = spool.get("filament") or {}
    vendor = filament.get("vendor") or {}
    for raw in (spool.get("spool_weight"), filament.get("spool_weight"), vendor.get("empty_spool_weight")):
        value = None if isinstance(raw, bool) else _safe_optional_float(raw)
        if value is not None:
            return value
    return SPOOLMAN_FALLBACK_TARE


def spoolman_label_weight(spool: dict) -> int:
    """The net weight of a full Spoolman spool in whole grams, as the list,
    the form and the weight syncs show it.

    The spool's own ``initial_weight`` before its filament's ``weight``
    (``spoolman_weights.spool_net_weight``, upstream #3194). A spool with no
    usable size keeps what these callers always read: the filament's value as
    stored (0 included — the AMS sync skips such a spool), else 1000.
    """
    net = spool_net_weight(spool)
    if net is not None:
        return _safe_int(net, 1000)
    return _safe_int((spool.get("filament") or {}).get("weight"), 1000)


def spoolman_price_weight(spool: dict) -> float:
    """The weight a Spoolman spool's price is converted at: its exact net
    weight, not the whole-gram label weight, so a 250.7 g spool is not
    re-priced by a no-op edit. A spool with no size keeps the old 1:1 reading
    of its price as a per-kg rate."""
    net = spool_net_weight(spool)
    return net if net is not None else 1000.0


def spoolman_cost_per_kg(spool: dict) -> float | None:
    """The spool form's cost per kg for a Spoolman spool.

    Spoolman's ``price`` on a spool is what that spool cost, not a rate; the
    form edits a rate, so the two are converted at the spool's net weight and
    agree only for a 1000 g spool (upstream #3194). Rounded to 4 places so a
    round trip through the form does not drift by float noise.
    """
    price = _safe_optional_float(spool.get("price"))
    if price is None or isinstance(spool.get("price"), bool):
        return None
    return round(price * 1000.0 / spoolman_price_weight(spool), 4)


def cost_per_kg_to_spoolman_price(cost_per_kg: float, net_weight: float) -> float:
    """Inverse of :func:`spoolman_cost_per_kg`: the price of a spool of ``net_weight`` grams."""
    return round(cost_per_kg * net_weight / 1000.0, 4)


def spoolman_effective_cost_per_kg(spool: dict) -> float | None:
    """The per-kg rate a print off this spool is charged, from the charge's
    own rule (``spoolman_weights.spool_price_per_gram``), so the Filaments
    page shows what prints cost."""
    per_gram = spool_price_per_gram(spool)
    return round(per_gram * 1000.0, 4) if per_gram is not None else None


# Spool.extra key holding the consumed-counter baseline: the value Spoolman's
# ``used_weight`` had when "Reset counter" was last pressed. The shown consumed
# figure is ``used_weight`` minus this — what internal mode does with its own
# ``weight_used_baseline`` column (upstream #2906). ``services/spoolman``
# declares the same name for the write side; a test pins the two equal.
BAMBU_WEIGHT_USED_BASELINE_KEY = "bambu_weight_used_baseline"


def _extract_extra_float(extra: dict, key: str) -> float | None:
    """A number stored in a Spoolman extra dict, or None.

    Spoolman keeps extra values as JSON text, and a ``text`` field accepts only
    a value that decodes to a string, so a number written through it arrives
    as ``'"263.0"'``; ``'263.0'`` and a bare number are read the same. None for
    a missing key, a non-finite value or anything that is not a number, so
    "no baseline recorded" stays apart from "a baseline of zero".
    """
    raw = extra.get(key)
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(raw, bool):
            return None
        if isinstance(raw, str):
            try:
                raw = float(raw)
            except ValueError:
                return None
    if not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def _extract_extra_str(extra: dict, key: str) -> str:
    """Extract a JSON-encoded string from a Spoolman extra dict.

    Spoolman stores extra values as JSON-stringified text — a stored string
    "GFL05" appears as `'"GFL05"'`. This unwraps that, returning the bare
    string. Returns "" for missing keys, non-strings, or invalid JSON.
    """
    raw = extra.get(key)
    if not isinstance(raw, str):
        return ""
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    return decoded if isinstance(decoded, str) else ""


def parse_spoolman_multi_colors(filament: dict) -> list[str]:
    """Spoolman's ``multi_color_hexes`` as a list of bare 6/8-char hex tokens.

    Spoolman stores the extra stops of a gradient / dual / multi-colour filament
    here, as a comma-separated string in some releases and a list in others;
    both shapes are accepted. Tokens keep their case and lose any leading ``#``
    — the form ``Spool.extra_colors`` stores and the client's ``parseStops``
    expects. Shared with the label renderer (``services/label_context``) so a
    printer card and a printed label never read the same field two ways
    (upstream #2967).
    """
    raw = filament.get("multi_color_hexes")
    if isinstance(raw, str):
        tokens = raw.split(",")
    elif isinstance(raw, list):
        tokens = [str(token) for token in raw]
    else:
        return []
    return [cleaned for token in tokens if (cleaned := token.strip().lstrip("#"))]


def _map_spoolman_spool(spool: dict) -> MappedSpoolFields:
    """Convert a raw Spoolman spool dict to the InventorySpool-compatible format."""
    raw_id = spool.get("id")
    if raw_id is None:
        raise ValueError("Spoolman spool is missing required 'id' field")
    try:
        spool_id: int = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Spoolman spool 'id' is not a valid integer: {raw_id!r}") from exc
    if spool_id <= 0:
        raise ValueError(f"Spoolman spool 'id' must be a positive integer, got {spool_id}")

    filament: dict = spool.get("filament") or {}
    if not filament:
        logger.warning(
            "Spoolman spool %s has no filament data — all filament fields will use defaults",
            spool_id,
        )
    vendor: dict = filament.get("vendor") or {}
    extra: dict = spool.get("extra") or {}

    raw_tag: str = (extra.get("tag") or "").strip('"').upper()
    _raw_is_hex = bool(_TAG_HEX_RE.match(raw_tag))
    tag_uid = raw_tag if _raw_is_hex and 8 <= len(raw_tag) <= 30 else None
    tray_uuid = raw_tag if _raw_is_hex and len(raw_tag) == 32 else None
    # Native tags (Spoolman 0.27+) fill whichever of the two extra.tag left
    # empty, so a spool linked in Spoolman itself shows as tagged here too.
    for native_uid in native_tag_uids(spool):
        if not _TAG_HEX_RE.match(native_uid):
            continue
        if len(native_uid) == 32:
            tray_uuid = tray_uuid or native_uid
        elif 8 <= len(native_uid) <= 30:
            tag_uid = tag_uid or native_uid

    material: str = (filament.get("material") or "").strip()
    filament_name: str = (filament.get("name") or "").strip()
    if material and filament_name.upper().startswith(material.upper()):
        subtype: str | None = filament_name[len(material) :].strip() or None
    else:
        subtype = filament_name or None

    raw_color = (filament.get("color_hex") or "").upper().removeprefix("#")
    color_hex: str = raw_color if _COLOR_HEX_RE.match(raw_color) else "808080"
    # An 8-char value already carries its alpha; appending the opaque byte
    # would push it to ten and lose the translucency it was stored to keep.
    rgba: str = color_hex if len(color_hex) == 8 else color_hex + "FF"
    # Spoolman carries the extra stops but has no concept of a surface effect:
    # its neighbouring field, ``multi_color_direction``, says how the stops are
    # laid out, not that the roll is silk or glitter. So a Spoolman spool
    # renders its gradient and never an effect overlay (upstream #2967).
    extra_stops = parse_spoolman_multi_colors(filament)
    extra_colors: str | None = ",".join(extra_stops) if extra_stops else None

    label_weight: int = spoolman_label_weight(spool)
    real_used_weight: float = _safe_float(spool.get("used_weight"), 0.0)
    # Parity with internal mode (#1390): the InventorySpool shape lets the
    # frontend compute ``remaining = label_weight - weight_used`` and
    # ``consumed = max(0, weight_used - weight_used_baseline)``. Map
    # Spoolman's two independent fields (``used_weight``, ``remaining_weight``)
    # onto that shape:
    #   ``weight_used``  = label_weight - remaining_weight  (so remaining matches)
    #   ``baseline``     = weight_used - used_weight        (so consumed matches)
    # When ``remaining_weight`` is unset (legacy spools, or filament linked
    # but never primed), fall back to the old behaviour: ``weight_used =
    # used_weight``, ``baseline = 0``.
    #
    # "Reset counter" records the then-current ``used_weight`` under
    # BAMBU_WEIGHT_USED_BASELINE_KEY in ``spool.extra`` and touches nothing
    # else; it is folded into the baseline here because the frontend always
    # shows ``weight_used - weight_used_baseline``. The reset used to PATCH
    # ``used_weight = 0`` instead, and Spoolman derives ``remaining_weight``
    # from initial minus used, so the spool jumped back to full (upstream
    # #2906). A negative value stored by hand is no baseline.
    stored = _extract_extra_float(extra, BAMBU_WEIGHT_USED_BASELINE_KEY)
    stored_baseline = 0.0 if stored is None else max(0.0, stored)
    remaining_raw = spool.get("remaining_weight")
    if remaining_raw is not None:
        remaining_weight: float = _safe_float(remaining_raw, 0.0)
        used_weight: float = max(0.0, float(label_weight) - remaining_weight)
        weight_used_baseline: float = max(0.0, used_weight - real_used_weight + stored_baseline)
    else:
        used_weight = real_used_weight
        weight_used_baseline = stored_baseline
    # The shown counter never reads negative: a weight sync can lower
    # ``used_weight`` below the baseline after a reset. It then reads 0 until
    # consumption climbs past the baseline again, as in internal mode.
    weight_used_baseline = min(weight_used_baseline, used_weight)

    archived: bool = spool.get("archived", False)
    archived_at: str | None = None
    if archived:
        archived_at = spool.get("last_used") or spool.get("registered") or None

    created_at: str | None = spool.get("registered") or None

    # Spoolman has no ``color_name`` field on Filament — confirmed against
    # the FilamentUpdateParameters schema in 0.23.1 (name/vendor_id/material/
    # price/density/diameter/weight/spool_weight/article_number/comment/
    # extruder_temp/bed_temp/color_hex/multi_color_hexes/multi_color_direction/
    # external_id/extra; no color_name). An earlier attempt to PATCH it was
    # silently dropped by Spoolman, which is exactly why user edits looked
    # "not saved" (upstream Bambuddy #1357 / commit 4a98914d).
    #
    # We persist color_name ourselves under ``spool.extra.bambu_color_name``
    # (JSON-encoded string, same pattern as ``bambu_slicer_filament``).
    # Read order:
    #   1. ``spool.extra.bambu_color_name`` — canonical store.
    #   2. ``filament.color_name`` — forward-compat for any future Spoolman
    #      release that adds the field, or for admins who registered the
    #      field themselves via a custom extra.
    #   3. ``subtype`` — synth fallback so the inventory list isn't a sea
    #      of "Unknown color" entries on installs with neither set.
    #
    # ``color_name_is_synthesized`` is True only when we fell back to
    # subtype — the edit form uses it to leave the input blank so the
    # user doesn't round-trip the synth value back as a real edit
    # (upstream #1319).
    extra_color_name = _extract_extra_str(extra, "bambu_color_name") or None
    stored_color_name = extra_color_name or (filament.get("color_name") or None)
    color_name: str | None = stored_color_name or subtype or None
    color_name_is_synthesized: bool = stored_color_name is None and color_name is not None

    # The drying record (services/spool_drying): JSON strings, empty = unknown.
    dried_temp = _safe_optional_float(_extract_extra_str(extra, "bambu_last_dried_temp") or None)
    dried_hours = _safe_optional_float(_extract_extra_str(extra, "bambu_last_dried_hours") or None)

    nozzle_temp_raw = filament.get("settings_extruder_temp")
    nozzle_temp_min: int | None = _safe_int(nozzle_temp_raw, 0) or None

    return {
        "id": spool_id,
        "material": material,
        "subtype": subtype,
        "color_name": color_name,
        "color_name_is_synthesized": color_name_is_synthesized,
        "rgba": rgba,
        "extra_colors": extra_colors,
        "effect_type": None,
        "brand": vendor.get("name") or None,
        "label_weight": label_weight,
        "core_weight": _safe_int(spoolman_tare(spool), 250),
        "core_weight_catalog_id": None,
        "weight_used": used_weight,
        "weight_used_baseline": weight_used_baseline,
        "weight_locked": False,
        "last_scale_weight": None,
        "last_weighed_at": None,
        # Slicer-preset extra is JSON-encoded under bambu_slicer_filament[_name].
        "slicer_filament": (_extract_extra_str(extra, "bambu_slicer_filament") or None),
        "slicer_filament_name": (_extract_extra_str(extra, "bambu_slicer_filament_name") or (filament_name or None)),
        "nozzle_temp_min": nozzle_temp_min,
        "nozzle_temp_max": None,
        "note": spool.get("comment") or None,
        "added_full": None,
        "last_used": spool.get("last_used"),
        "last_dried_at": _extract_extra_str(extra, "bambu_last_dried_at") or None,
        "last_dried_temp": int(round(dried_temp)) if dried_temp else None,
        "last_dried_hours": dried_hours or None,
        "encode_time": spool.get("first_used"),
        "tag_uid": tag_uid,
        "tray_uuid": tray_uuid,
        "data_origin": "spoolman",
        "tag_type": "spoolman",
        "archived_at": archived_at,
        "created_at": created_at,
        "updated_at": created_at,
        # Spoolman's spool price is what the whole spool cost (upstream #3194).
        "cost_per_kg": spoolman_cost_per_kg(spool),
        "effective_cost_per_kg": spoolman_effective_cost_per_kg(spool),
        "storage_location": spool.get("location") or None,
        "location_id": None,
        "k_profiles": [],
    }
