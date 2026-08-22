"""Cloud push of authored families (spec B §5). One interface per
ecosystem; Bambu ships active, Orca is designed-inactive (blocked on the
external write-scope / re-pairing / own-client_id dependency)."""

from __future__ import annotations

PUSH_CAPABLE = {"bambu": True, "orca": False}


async def push_family(db, *, filament_id: str, ecosystem: str = "bambu", user=None) -> list[dict]:
    raise NotImplementedError  # implemented in the push task
