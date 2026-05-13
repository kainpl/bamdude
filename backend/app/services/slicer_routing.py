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
import time
from typing import Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings as app_settings

logger = logging.getLogger(__name__)

SlicerKind = Literal["orcaslicer", "bambu_studio"]
_VALID_SLICERS: tuple[SlicerKind, ...] = ("orcaslicer", "bambu_studio")

# Module-level "is any sidecar online?" cache. 30s TTL is enough to dampen
# capability fetches during a wizard open without making stale-state lag
# feel obvious. Separate from slicer_presets.py::_health_cache (which keys
# on resolved URL) because we only care about the OR-of-both answer here.
_ANY_SIDECAR_TTL_SECONDS = 30.0
_any_sidecar_cache: tuple[float, bool] | None = None


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


async def any_sidecar_online(db: AsyncSession) -> bool:
    """Probe both slicer sidecars and return True if at least one ``/health`` is 2xx.

    Used by the calibration capabilities endpoint to gate STL-based modes
    (PA Line / PA Tower / Temp / VolSpeed / VFA / Retraction) — those need
    a connected sidecar for the Wave 2 slicing pipeline. Result cached
    30 s module-wide to keep the wizard's capability poll off the wire.

    Honours the ``use_slicer_api`` master toggle — when the operator turns
    that off in Settings, BamDude pretends no sidecar exists even if a
    reachable URL is configured (matches the rest of the app: SliceModal
    et al. hide their slicer UI behind the same flag). The toggle check
    runs *before* the cache so flipping the switch in Settings shows up
    in the wizard immediately rather than after the 30 s TTL.

    Returns False on any failure (toggle off, network, non-2xx,
    unconfigured URL) — "available" must be unambiguously true, never
    best-guess.
    """
    from backend.app.api.routes.settings import get_setting

    use_api = await get_setting(db, "use_slicer_api")
    # SettingsService stores bools as JSON strings; treat unset / falsy as off.
    if str(use_api or "").lower() not in ("true", "1", "yes"):
        return False

    global _any_sidecar_cache
    now = time.monotonic()
    if _any_sidecar_cache and (now - _any_sidecar_cache[0]) < _ANY_SIDECAR_TTL_SECONDS:
        return _any_sidecar_cache[1]

    urls: list[str] = []
    for kind in _VALID_SLICERS:
        _, url = await resolve_sidecar_url(db, slicer_override=kind)
        if url:
            urls.append(url)

    online = False
    if urls:
        async with httpx.AsyncClient(timeout=2.0) as client:
            for url in urls:
                try:
                    response = await client.get(f"{url}/health")
                    if 200 <= response.status_code < 300:
                        online = True
                        break
                except httpx.RequestError:
                    continue

    _any_sidecar_cache = (now, online)
    return online
