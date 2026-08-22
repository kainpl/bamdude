"""Authoring of custom filament families (spec B) — BamDude's analog of
BS's Create Filament dialog: vendor+type+serial -> a P-hash family + one
root LocalPreset per chosen printer, absorbed into the spec-A mirrors so
slots / K-profiles / slicing see it like any family.

BS parity (temp/references/BambuStudio/src/slic3r/GUI/CreatePresetsDialog.cpp):
- special_key strip set, vendor refusals, the fixed filament-type list;
- get_filament_id: pre-'@' name match adopts the existing filament_id,
  otherwise "P"+md5(name)[:7] (logged-out form — no user-id salt: the
  family lives in BamDude and on printers, and BS's own name-search
  convergence makes the hash push-compatible), collision with a different
  name re-hashes with a timestamp appended.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.utils import filament_catalog as catalog

logger = logging.getLogger(__name__)

# BS CreatePresetsDialog.cpp:43 — order preserved, the literal duplicate
# "PETG" removed.
FILAMENT_TYPES = [
    "PLA", "PLA+", "PLA Tough", "PETG", "ABS", "ASA", "FLEX", "HIPS", "PA", "PACF",
    "NYLON", "PVA", "PC", "PCABS", "PCTG", "PCCF", "PP", "PEI", "PET",
    "PETGCF", "PTBA", "PTBA90A", "PEEK", "TPU93A", "TPU75D", "TPU", "TPU-AMS",
    "TPU92A", "TPU98A", "Misc", "TPE", "GLAZE", "Nylon", "CPE", "METAL", "ABST",
    "Carbon Fiber",
]  # fmt: skip

_SPECIAL_KEYS = set("\n\t\r\v@;")  # BS special_key set


class AuthoringError(ValueError):
    """Caller-facing authoring failure (400 at the route layer)."""


class FamilyInUseError(AuthoringError):
    """Deletion refused: spools / calibrations still reference the id (409)."""

    def __init__(self, spools: int, calibrations: int):
        super().__init__(f"family is referenced by {spools} spool(s) and {calibrations} calibration(s)")
        self.spools = spools
        self.calibrations = calibrations


def strip_special_keys(s: str) -> str:
    return "".join(c for c in s if c not in _SPECIAL_KEYS)


def validate_vendor(vendor: str) -> str:
    v = strip_special_keys(vendor or "").strip()
    if not v:
        raise AuthoringError("vendor is required")
    # BS compares case-sensitively; we refuse any casing — "bambu PLA x"
    # differing from the reserved vendor only by case would mislead.
    if v.lower() in ("bambu", "generic"):
        raise AuthoringError(f'vendor "{v}" is reserved')
    if v.isdigit():
        raise AuthoringError("vendor cannot be digits only")
    return v


def build_family_name(vendor: str, filament_type: str, serial: str) -> str:
    if filament_type not in FILAMENT_TYPES:
        raise AuthoringError(f"unknown filament type {filament_type!r}")
    s = strip_special_keys(serial or "").strip()
    if not s:
        raise AuthoringError("serial is required")
    return f"{validate_vendor(vendor)} {filament_type} {s}"


def _alias_of(name: str) -> str:
    return name.split("@")[0].strip() if "@" in name else name.strip()


async def _known_names(db: AsyncSession) -> dict[str, set[str]]:
    """filament_id -> pre-'@' names, across catalog + mirrors + families —
    BS get_filament_id walks system+user preset bundles the same way."""
    known: dict[str, set[str]] = {}
    for fam in catalog.search_families("", 100000):
        known.setdefault(fam.filament_id, set()).add(fam.alias)
    for row in (await db.execute(select(UserFilamentPreset))).scalars():
        if row.family_filament_id:
            known.setdefault(row.family_filament_id, set()).add(_alias_of(row.name))
    for fam in (await db.execute(select(UserFilamentFamily))).scalars():
        known.setdefault(fam.filament_id, set()).add(fam.alias)
    return known


async def mint_filament_id(db: AsyncSession, family_name: str) -> tuple[str, bool]:
    """Return ``(filament_id, attached)``. A known name adopts its existing
    id (BS convergence / spec §1 dedup); otherwise mint the logged-out
    P-hash, re-hashing while the id is taken by a different name."""
    known = await _known_names(db)
    for fid, names in known.items():
        if family_name in names:
            return fid, True
    fid = "P" + hashlib.md5(family_name.encode("utf-8")).hexdigest()[:7]
    while fid in known:  # taken by a DIFFERENT name (same name returned above)
        salted = f"{family_name}{datetime.now(timezone.utc).isoformat()}"
        fid = "P" + hashlib.md5(salted.encode("utf-8")).hexdigest()[:7]
    return fid, False
