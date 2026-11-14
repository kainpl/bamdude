"""One slicer preset, flattened along its ``inherits`` chain, for "Assemble project".

Presets come through the existing subsystem only: ``preset_resolver.resolve_preset_ref`` answers what the
user picked (a system stub, a cloud delta, a local preset) and ``orca_profiles.resolve_preset`` walks the
parents. No slicer sidecar is involved. Unlike the lenient callers of that walker, assembly must not write a
project from half a preset: a parent that cannot be found is :class:`PresetUnresolved`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.user import User
from backend.app.schemas.slicer import PresetRef
from backend.app.services.orca_profiles import MissingBaseProfile, fetch_and_cache_base_profile, resolve_preset
from backend.app.services.preset_resolver import resolve_preset_ref

Slot = Literal["printer", "process", "filament"]

# Keys that describe the preset itself rather than a setting; never copied into a project.
META_KEYS = frozenset(
    {
        "name",
        "inherits",
        "from",
        "type",
        "setting_id",
        "base_id",
        "instantiation",
        "filament_id",
        "description",
        "include",
        "compatible_printers",
        "compatible_printers_condition",
        "compatible_prints",
        "compatible_prints_condition",
    }
)

_WALKER_TYPE = {"printer": "machine", "process": "process", "filament": "filament"}


class PresetUnresolved(Exception):
    """A preset's own content or one of its parents could not be obtained."""

    def __init__(self, slot: str, name: str, missing: str):
        super().__init__(f"{slot} preset {name!r} cannot be resolved: profile {missing!r} was not found")
        self.slot = slot
        self.name = name
        self.missing = missing


@dataclass(frozen=True)
class FlatPreset:
    slot: Slot
    name: str  # the preset's own name
    flat: dict  # every key, parents merged under children
    parent: str  # the name it inherits from; "" for a root or a system preset
    changed: tuple[str, ...]  # setting keys whose value differs from the flattened parent, sorted


_MAX_PARENTS = 16


async def _warm(raw: dict, walker_type: str) -> None:
    """Download the parents nobody cached yet, each in a transaction of its own.

    The walker caches what it fetches on the session it is given; done on the caller's session, the first
    cached parent would be held — on SQLite, with the database's one write lock — across every later
    download. Here each parent is fetched and committed alone, so the caller's walk finds them all cached.
    A parent that cannot be fetched is left for the walk to report.
    """
    from backend.app.core import database  # read at call time: the session factory is replaced under test

    name, seen = raw.get("inherits"), set()
    while isinstance(name, str) and name and name not in seen and len(seen) < _MAX_PARENTS:
        seen.add(name)
        async with database.async_session() as own:
            parent = await fetch_and_cache_base_profile(name, walker_type, own)
            await own.commit()
        if not isinstance(parent, dict):
            return
        name = parent.get("inherits")


async def flatten(
    db: AsyncSession,
    user: User | None,
    ref: PresetRef,
    slot: Slot,
    *,
    credential_snapshots: list,
) -> FlatPreset:
    """Resolve ``ref`` and flatten it. The caller commits afterwards: the walker caches fetched parents.

    Raises ``HTTPException`` for what ``resolve_preset_ref`` refuses (unknown id, missing cloud right) and
    :class:`PresetUnresolved` when the chain cannot be completed.
    """
    raw = json.loads(await resolve_preset_ref(db, user, ref, slot, credential_snapshots=credential_snapshots))
    if not isinstance(raw, dict):
        raise PresetUnresolved(slot, str(ref.id), str(ref.id))
    name = str(raw.get("name") or ref.id)
    walker_type = _WALKER_TYPE[slot]
    await _warm(raw, walker_type)
    try:
        flat = await resolve_preset(raw, walker_type, db, strict=True)
        if ref.source == "standard":
            # the stub inherits from itself: it IS the system preset, with nothing changed
            return FlatPreset(slot, name, flat, "", ())
        parent = str(raw.get("inherits") or "")
        if not parent:
            return FlatPreset(slot, name, flat, "", ())
        base = await resolve_preset({"inherits": parent}, walker_type, db, strict=True)
    except MissingBaseProfile as error:
        raise PresetUnresolved(slot, name, error.name) from None
    changed = tuple(sorted(key for key, value in flat.items() if key not in META_KEYS and base.get(key) != value))
    return FlatPreset(slot, name, flat, parent, changed)
