"""Shared helpers for picking which slicer-API sidecar to talk to.

The same routing decision -- "OrcaSlicer or BambuStudio sidecar?" -- shows up
in four places (the slice routes in ``library.py`` and ``archives.py``, the
preview-slice helpers, and the unified preset listing). This module owns
the rule so a future change (e.g. adding a third slicer family, or making
the preferred-slicer fallback per-user) lands in one file.

Routing rule:

1. ``slicer_override`` from the caller wins. Used by the slice routes when
   the user picked a specific slicer in the SliceModal and the picked one
   isn't the global default. Must be one of ``"orcaslicer"`` /
   ``"bambu_studio"``; anything else falls through to the setting.
2. The ``preferred_slicer`` setting (default ``"bambu_studio"``) decides
   the global default.
3. The per-slicer URL setting (``orcaslicer_api_url`` /
   ``bambu_studio_api_url``) wins over the env default
   (``SLICER_API_URL`` / ``BAMBU_STUDIO_API_URL``). Empty setting falls
   through to env.
4. Returns ``None`` when the chosen URL is empty in both setting and env.
   Callers decide whether to surface that as a 400/503 or fall back to a
   heuristic.
"""

from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings as app_settings

logger = logging.getLogger(__name__)

SlicerKind = Literal["orcaslicer", "bambu_studio"]
_VALID_SLICERS: tuple[SlicerKind, ...] = ("orcaslicer", "bambu_studio")


async def resolve_sidecar_url(
    db: AsyncSession,
    *,
    slicer_override: str | None = None,
) -> tuple[SlicerKind | None, str | None]:
    """Resolve which slicer the request should target and where its sidecar lives.

    Returns ``(chosen_slicer, api_url)``:

    - ``chosen_slicer`` is the canonical kind name that was selected
      (``"orcaslicer"`` or ``"bambu_studio"``), or ``None`` when the
      preference setting is malformed.
    - ``api_url`` is the resolved sidecar URL (per-slicer setting wins
      over env default), or ``None`` when both are empty.
    """
    from backend.app.api.routes.settings import get_setting

    chosen: SlicerKind | None
    if slicer_override in _VALID_SLICERS:
        chosen = slicer_override  # type: ignore[assignment]
    else:
        preferred = (await get_setting(db, "preferred_slicer")) or "bambu_studio"
        if preferred not in _VALID_SLICERS:
            logger.warning("Unknown preferred_slicer setting: %r", preferred)
            return None, None
        chosen = preferred  # type: ignore[assignment]

    if chosen == "orcaslicer":
        configured = await get_setting(db, "orcaslicer_api_url")
        url = (configured or app_settings.slicer_api_url).strip()
    else:  # bambu_studio
        configured = await get_setting(db, "bambu_studio_api_url")
        url = (configured or app_settings.bambu_studio_api_url).strip()

    return chosen, (url or None)


def slicer_label(kind: SlicerKind) -> str:
    """Human-readable label for an error message ("OrcaSlicer" / "BambuStudio")."""
    return "OrcaSlicer" if kind == "orcaslicer" else "BambuStudio"
