"""Pure helper functions for Spoolman spool mapping.

No heavy dependencies — importable in unit tests without the full backend stack.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
from typing import Any
from urllib.parse import urlparse

from typing_extensions import TypedDict

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
    encode_time: str | None
    tag_uid: str | None
    tray_uuid: str | None
    data_origin: str | None
    tag_type: str | None
    archived_at: str | None
    created_at: str | None
    updated_at: str | None
    cost_per_kg: float | None
    storage_location: str | None
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


_CLOUD_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


def assert_safe_spoolman_url(url: str) -> None:
    """Raise ValueError if *url* should be blocked as an SSRF risk.

    BamDude is typically deployed on a home LAN alongside Spoolman, so
    loopback (127.0.0.1) and RFC-1918 private ranges (192.168.x.x, 10.x.x.x,
    172.16-31.x) must be permitted — they are THE normal Spoolman topology.
    This guard therefore targets the genuinely dangerous cases only.

    Checks performed:
    - Scheme must be http or https.
    - Numeric-encoded IP addresses (decimal / hex) are rejected.
    - Cloud-metadata endpoints (169.254.169.254, 100.100.100.200, fd00:ec2::254).
    - Multicast / unspecified addresses.
    - IPv4-mapped IPv6 unwrapped before checking.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("Spoolman URL must use http or https")

    hostname = (parsed.hostname or "").lower()

    if re.match(r"^(0x[0-9a-f]+|[0-9]+)$", hostname, re.I):
        raise ValueError("Spoolman URL must not use numeric-encoded IP addresses; use standard dotted-decimal notation")

    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return

    effective: ipaddress.IPv4Address | ipaddress.IPv6Address = addr
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        effective = addr.ipv4_mapped

    if effective in _CLOUD_METADATA_IPS:
        raise ValueError("Spoolman URL must not point to a cloud metadata endpoint")

    if effective.is_multicast or effective.is_unspecified:
        raise ValueError("Spoolman URL must not point to a multicast or unspecified address")


_COLOR_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
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

    material: str = (filament.get("material") or "").strip()
    filament_name: str = (filament.get("name") or "").strip()
    if material and filament_name.upper().startswith(material.upper()):
        subtype: str | None = filament_name[len(material) :].strip() or None
    else:
        subtype = filament_name or None

    raw_color = (filament.get("color_hex") or "").upper().removeprefix("#")
    color_hex: str = raw_color if _COLOR_HEX_RE.match(raw_color) else "808080"
    rgba: str = color_hex + "FF"

    label_weight: int = _safe_int(filament.get("weight"), 1000)
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
    remaining_raw = spool.get("remaining_weight")
    if remaining_raw is not None:
        remaining_weight: float = _safe_float(remaining_raw, 0.0)
        used_weight: float = max(0.0, float(label_weight) - remaining_weight)
        weight_used_baseline: float = max(0.0, used_weight - real_used_weight)
    else:
        used_weight = real_used_weight
        weight_used_baseline = 0.0

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

    nozzle_temp_raw = filament.get("settings_extruder_temp")
    nozzle_temp_min: int | None = _safe_int(nozzle_temp_raw, 0) or None

    return {
        "id": spool_id,
        "material": material,
        "subtype": subtype,
        "color_name": color_name,
        "color_name_is_synthesized": color_name_is_synthesized,
        "rgba": rgba,
        "brand": vendor.get("name") or None,
        "label_weight": label_weight,
        "core_weight": _safe_int(
            spool.get("spool_weight") if spool.get("spool_weight") is not None else filament.get("spool_weight"), 250
        ),
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
        "encode_time": spool.get("first_used"),
        "tag_uid": tag_uid,
        "tray_uuid": tray_uuid,
        "data_origin": "spoolman",
        "tag_type": "spoolman",
        "archived_at": archived_at,
        "created_at": created_at,
        "updated_at": created_at,
        "cost_per_kg": _safe_optional_float(spool.get("price")),
        "storage_location": spool.get("location") or None,
        "k_profiles": [],
    }
