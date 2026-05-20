"""Unified slicer-preset listing for the SliceModal.

Returns the printer / process / filament options grouped by source tier in
priority order — cloud (per-user, live-fetched) > local (DB-backed
imports) > standard (slicer-bundled stock fallback). Name-based dedup is
applied so a preset that exists in multiple tiers only appears in the
highest-priority one. Cloud failure modes (signed out / expired / network)
are surfaced via a status field so the modal can render a precise banner
without faking an "ok with empty list" response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.cloud import get_stored_token, resolve_api_key_cloud_owner
from backend.app.core.auth import require_permission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.local_preset import LocalPreset
from backend.app.models.user import User
from backend.app.schemas.slicer_presets import (
    FilamentPresetInfo,
    UnifiedPreset,
    UnifiedPresetsBySlot,
    UnifiedPresetsResponse,
)
from backend.app.services.bambu_cloud import (
    BambuCloudAuthError,
    BambuCloudError,
    BambuCloudService,
)
from backend.app.services.slicer_api import (
    BundleNotFoundError,
    BundleSummary,
    SlicerApiError,
    SlicerApiServerError,
    SlicerApiService,
    SlicerApiUnavailableError,
    SlicerInputError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slicer", tags=["Slicer Presets"])


# In-process cache for the bundled-profile list. The slicer sidecar walks a
# read-only filesystem inside its own container, so the list only changes
# across sidecar rebuilds — a long TTL is safe and avoids a sidecar round-trip
# on every modal open. Per-user cache is unnecessary because bundled profiles
# are global.
_BUNDLED_TTL_S = 3600.0
_bundled_cache: tuple[float, dict[str, list[UnifiedPreset]]] | None = None

# Per-user cache for the cloud preset list. Cache key is (user_id, token_hash):
# keying on the token hash means a logout/login or token-change automatically
# invalidates the entry without needing the cloud-auth route handlers to call
# back into this module. 5 minutes balances "users see their freshly-saved
# presets quickly" against "a busy install doesn't hit the cloud once per
# modal open per user".
_CLOUD_TTL_S = 300.0
_cloud_cache: dict[tuple[int, str], tuple[float, dict[str, list[UnifiedPreset]]]] = {}


def _token_fingerprint(token: str) -> str:
    """Short stable hash of the cloud token for use as a cache-key component.
    Storing only the hash means we can safely keep multiple per-(user, token)
    entries without leaking the token via the in-process dict."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


_CLOUD_TYPE_TO_SLOT = {
    "filament": "filament",
    "printer": "printer",
    "print": "process",  # Bambu Cloud calls process presets "print"
}


def _empty_slots() -> dict[str, list[UnifiedPreset]]:
    return {"printer": [], "process": [], "filament": []}


async def _fetch_cloud_presets(db: AsyncSession, user: User | None) -> tuple[dict[str, list[UnifiedPreset]], str]:
    """Return ``(slots, cloud_status)``. Slots are empty when ``cloud_status != 'ok'``.

    Defence-in-depth: even if a stored ``cloud_token`` survived a permission
    revocation (admin reset, legacy state), users without ``CLOUD_AUTH`` are
    treated as not-authenticated for this endpoint — the cloud tier never
    surfaces for them. This keeps the per-tier visibility consistent with the
    /cloud/* endpoint suite that already gates on ``CLOUD_AUTH``.
    """
    if user is not None and not user.has_permission(Permission.CLOUD_AUTH.value):
        return _empty_slots(), "not_authenticated"

    token, _email, region = await get_stored_token(db, user)
    if not token:
        return _empty_slots(), "not_authenticated"

    user_key = user.id if user is not None else 0
    cache_key = (user_key, _token_fingerprint(token))
    now = time.monotonic()
    cached = _cloud_cache.get(cache_key)
    if cached and now - cached[0] < _CLOUD_TTL_S:
        return cached[1], "ok"

    cloud = BambuCloudService(region=region)
    cloud.set_token(token)
    try:
        try:
            raw = await cloud.get_slicer_settings()
        except BambuCloudAuthError:
            return _empty_slots(), "expired"
        except BambuCloudError as e:
            logger.warning("Cloud preset fetch failed for user %s: %s", user_key, e)
            return _empty_slots(), "unreachable"
        except Exception as e:  # noqa: BLE001 — defensive: never crash the modal
            logger.warning("Cloud preset fetch unexpected error for user %s: %s", user_key, e)
            return _empty_slots(), "unreachable"

        slots = _empty_slots()
        for cloud_type, slot in _CLOUD_TYPE_TO_SLOT.items():
            type_data = raw.get(cloud_type, {})
            # The cloud splits presets into "private" (the user's own) and
            # "public" (Bambu's stock cloud presets). Both are valid choices
            # — surface them in the natural order private → public so a user's
            # customisations appear above the stock entries with the same
            # names. Stock entries that share names with private ones get
            # deduped out within the cloud tier itself.
            seen_names: set[str] = set()
            for entry in type_data.get("private", []) + type_data.get("public", []):
                name = entry.get("name")
                setting_id = entry.get("setting_id") or entry.get("id")
                if not name or not setting_id or name in seen_names:
                    continue
                seen_names.add(name)
                slots[slot].append(UnifiedPreset(id=setting_id, name=name, source="cloud"))

        # Cloud filament presets carry no metadata in this response on
        # purpose: the per-preset detail endpoint
        # (/v1/iot-service/api/slicer/setting/{id}) is rate-limited at roughly
        # 10/sec per token, so fetching N filament presets to enrich them
        # one-by-one trips Bambu's limiter and returns 429 on every request
        # for users with large preset libraries.
        #
        # The dedup pass (see _dedupe_by_name) compensates: when a cloud entry
        # wins over a same-named local entry, the cloud entry inherits the
        # local entry's filament_type / filament_colour. So cloud presets that
        # also exist locally still get metadata-aware pre-pick in the
        # SliceModal; cloud-only presets fall back to plain priority order.
        _cloud_cache[cache_key] = (now, slots)
        return slots, "ok"
    finally:
        await cloud.close()


async def _fetch_local_presets(db: AsyncSession) -> dict[str, list[UnifiedPreset]]:
    """Local imports — no caching needed, single indexed DB read."""
    result = await db.execute(select(LocalPreset).order_by(LocalPreset.name))
    presets = result.scalars().all()
    slots = _empty_slots()
    type_to_slot = {"filament": "filament", "printer": "printer", "process": "process"}
    for p in presets:
        slot = type_to_slot.get(p.preset_type)
        if slot is None:
            continue
        extra: dict[str, str | float | None] = {}
        if slot == "filament":
            extra["filament_type"], extra["filament_colour"] = _parse_filament_metadata(p.setting)
            extra["filament_flow_ratio"] = _parse_filament_flow_ratio(p.setting)
        slots[slot].append(
            UnifiedPreset(id=str(p.id), name=p.name, source="local", **extra),
        )
    return slots


def _parse_filament_metadata(setting_json: str | None) -> tuple[str | None, str | None]:
    """Extract first-slot ``filament_type`` and ``filament_colour`` from a
    stored preset JSON. OrcaSlicer stores both as arrays (per-extruder) — we
    take the first entry since pre-pick matching is one-slot-at-a-time.
    Defensive parse: any error returns ``(None, None)`` so a corrupt row never
    breaks the listing."""
    if not setting_json:
        return None, None
    try:
        data = json.loads(setting_json)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    return _first_scalar(data.get("filament_type")), _first_scalar(data.get("filament_colour"))


def _parse_filament_flow_ratio(setting_json: str | None) -> float | None:
    """Extract the filament preset's stored ``filament_flow_ratio`` so the
    Flow Rate verify-download page can auto-prefill the baseline input
    with the operator's current value (saves them from typing it). BS
    stores it as a per-extruder vector; we surface the first scalar.
    Any parse failure returns ``None`` so a corrupt row never breaks the
    listing."""
    if not setting_json:
        return None
    try:
        data = json.loads(setting_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("filament_flow_ratio")
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, (int, float)) and float(raw) > 0:
        return float(raw)
    if isinstance(raw, str):
        try:
            v = float(raw)
        except ValueError:
            return None
        return v if v > 0 else None
    return None


def _first_scalar(value: object) -> str | None:
    if isinstance(value, list) and value:
        return value[0] if isinstance(value[0], str) else None
    if isinstance(value, str) and value:
        return value
    return None


async def _fetch_bundled_presets(db: AsyncSession) -> dict[str, list[UnifiedPreset]]:
    """Standard slicer-bundled profiles via the sidecar's ``/profiles/bundled``."""
    global _bundled_cache
    now = time.monotonic()
    if _bundled_cache and now - _bundled_cache[0] < _BUNDLED_TTL_S:
        return _bundled_cache[1]

    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        # No sidecar configured at all — return empty rather than caching, so
        # users who configure one mid-session see results on next open.
        return _empty_slots()

    try:
        async with SlicerApiService(base_url=api_url) as svc:
            raw = await svc.list_bundled_profiles()
    except SlicerApiError as e:
        logger.info("Bundled preset fetch from sidecar at %s failed: %s", api_url, e)
        return _empty_slots()
    except Exception as e:  # noqa: BLE001 — never break the modal on sidecar issues
        logger.warning("Bundled preset fetch unexpected error: %s", e)
        return _empty_slots()

    slots = _empty_slots()
    for slot in ("printer", "process", "filament"):
        for entry in raw.get(slot, []) or []:
            name = entry.get("name")
            if not name:
                continue
            # Bundled presets are addressed by name (the slicer resolves them
            # by name during the ``inherits:`` walk), so name doubles as id.
            extra: dict[str, str | None] = {}
            if slot == "filament":
                extra["filament_type"] = entry.get("filament_type")
                extra["filament_colour"] = entry.get("filament_colour")
            slots[slot].append(
                UnifiedPreset(id=name, name=name, source="standard", **extra),
            )

    _bundled_cache = (now, slots)
    return slots


async def _resolve_slicer_api_url(db: AsyncSession) -> str | None:
    """Pick the sidecar URL the bundled-listing fetch should hit.

    Thin wrapper over :func:`backend.app.services.slicer_routing.resolve_sidecar_url`
    -- the bundled-tier listing always uses the global preferred slicer
    (it has no per-request context to read a slicer override from).
    Returns ``None`` (empty bundled tier) on misconfiguration rather than
    raising, since the modal's preset listing is informational.
    """
    from backend.app.services.slicer_routing import resolve_sidecar_url

    _, url = await resolve_sidecar_url(db)
    return url


def _dedupe_by_name(
    cloud: dict[str, list[UnifiedPreset]],
    local: dict[str, list[UnifiedPreset]],
    standard: dict[str, list[UnifiedPreset]],
) -> tuple[
    dict[str, list[UnifiedPreset]],
    dict[str, list[UnifiedPreset]],
    dict[str, list[UnifiedPreset]],
]:
    """Filter so each preset name appears in exactly one tier
    (cloud > local > standard).

    Order within each tier is preserved as-is — only "lower-priority duplicates"
    are dropped. A preset shared across tiers (e.g. "Bambu PLA Basic" in cloud
    public AND standard bundled) only renders once, in the cloud tier.

    Filament metadata is **merged across tiers** during dedup: when a cloud
    entry wins over a same-named local entry, the cloud entry inherits the
    local entry's ``filament_type`` and ``filament_colour`` (cloud entries
    carry no metadata themselves because we deliberately don't fetch each
    setting's content — see ``_fetch_cloud_presets``). Without this merge,
    the SliceModal's metadata-aware pre-pick would silently lose match data
    for every preset the user has both cloud-synced and locally imported, and
    fall back to plain priority selection.
    """
    metadata_by_name: dict[str, tuple[str | None, str | None]] = {}
    for tier in (local, standard):
        for p in tier["filament"]:
            if p.name in metadata_by_name:
                continue
            if p.filament_type or p.filament_colour:
                metadata_by_name[p.name] = (p.filament_type, p.filament_colour)

    for p in cloud["filament"]:
        if (p.filament_type is None or p.filament_colour is None) and p.name in metadata_by_name:
            t, c = metadata_by_name[p.name]
            if p.filament_type is None and t is not None:
                p.filament_type = t
            if p.filament_colour is None and c is not None:
                p.filament_colour = c

    deduped_local = _empty_slots()
    deduped_standard = _empty_slots()
    for slot in ("printer", "process", "filament"):
        seen = {p.name for p in cloud[slot]}
        for p in local[slot]:
            if p.name in seen:
                continue
            deduped_local[slot].append(p)
            seen.add(p.name)
        for p in standard[slot]:
            if p.name in seen:
                continue
            deduped_standard[slot].append(p)
            seen.add(p.name)
    return cloud, deduped_local, deduped_standard


@router.get("/presets", response_model=UnifiedPresetsResponse)
async def list_unified_presets(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
) -> UnifiedPresetsResponse:
    """List slicer presets across cloud / local / standard tiers, deduped by name.

    Drives the SliceModal preset dropdowns. Permission gate matches the
    slice action itself (``LIBRARY_UPLOAD``) so any user who can slice can
    see the preset options for the dialog. The cloud branch is independently
    gated on ``CLOUD_AUTH`` inside :func:`_fetch_cloud_presets` so a user
    with only ``LIBRARY_UPLOAD`` doesn't see cloud presets they shouldn't
    have access to.

    For API-key callers (no JWT user) with ``can_access_cloud=True`` set on
    the key, ``api_key_cloud_owner`` resolves to the key's owner so cloud
    presets surface against that user's stored Bambu Cloud token (#1182).
    Without an API-key cloud-owner, the cloud tier remains empty (the same
    behaviour as a JWT request from a user with no cloud token).
    """
    cloud_token_user = current_user or api_key_cloud_owner
    cloud, cloud_status = await _fetch_cloud_presets(db, cloud_token_user)
    local = await _fetch_local_presets(db)
    standard = await _fetch_bundled_presets(db)

    cloud, local, standard = _dedupe_by_name(cloud, local, standard)

    return UnifiedPresetsResponse(
        cloud=UnifiedPresetsBySlot(**cloud),
        local=UnifiedPresetsBySlot(**local),
        standard=UnifiedPresetsBySlot(**standard),
        cloud_status=cloud_status,
    )


@router.get("/filament-preset/info", response_model=FilamentPresetInfo)
async def get_filament_preset_info(
    source: str,
    id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_permission(Permission.PRINTERS_READ)),
) -> FilamentPresetInfo:
    """Resolve a filament preset's flow-rate-relevant metadata on demand.

    The ``/slicer/presets`` listing is thin (id + name + filament_type +
    colour) — for cloud / standard sources the listing doesn't expose
    ``filament_flow_ratio`` because resolving every entry to its full JSON
    on every modal open would hit Bambu Cloud N times. This endpoint
    resolves a single picked preset via :func:`resolve_preset_ref` and
    returns the flow-rate fields the Flow Rate verify-download page
    needs to auto-prefill the pass-1 baseline. Per-call cost = one
    cloud-detail fetch (cloud), one DB read (local), or no I/O
    (standard's stub). Frontend caches the result per ``(source, id)``.
    """
    from backend.app.schemas.slicer import PresetRef
    from backend.app.services.preset_resolver import resolve_preset_ref

    if source not in ("cloud", "local", "standard"):
        raise HTTPException(400, f"Invalid source {source!r}")
    ref = PresetRef(source=source, id=id)  # type: ignore[arg-type]
    try:
        content = await resolve_preset_ref(db, user, ref, slot="filament")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — never break the modal on a resolve failure
        logger.warning("filament-preset/info resolve failed for %s/%s: %s", source, id, e)
        return FilamentPresetInfo()

    flow_ratio = _parse_filament_flow_ratio(content)
    filament_type, _ = _parse_filament_metadata(content)
    return FilamentPresetInfo(flow_ratio=flow_ratio, filament_type=filament_type)


# Per-slicer health cache: ``{(kind, url): (timestamp, payload)}``. 30 s TTL
# keeps a tab's modal-render-time check off the wire while the user clicks
# around, but is still tight enough that a sidecar that just came up (e.g.
# user ran ``docker compose up`` after opening the modal) becomes available
# within half a minute. Cache is process-local; a fleet running multiple
# BamDude workers re-checks per-worker, which is fine -- the hit is one
# 5 s-timeout HTTP GET.
_HEALTH_TTL_SECONDS = 30.0
_health_cache: dict[tuple[str, str], tuple[float, dict]] = {}


@router.get("/health/{slicer}")
async def get_slicer_health(
    slicer: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Probe a sidecar's ``GET /health`` endpoint and return its status.

    Used by the SliceModal radio to dim an unreachable slicer option and
    by Settings → Profiles → Slicer API to render a green/red indicator
    next to each URL field. ``slicer`` must be ``orcaslicer`` or
    ``bambu_studio``.

    Response shape:

    - ``{healthy: true, version: "2.3.2"}`` when the sidecar replied 2xx.
    - ``{healthy: false, error: "<message>", url: "..."}`` when the URL
      is empty, the sidecar is unreachable, or it replied non-2xx. URL
      is included so the modal can show "BambuStudio at <url> unreachable".

    Cached 30 s per ``(slicer, resolved_url)`` to keep the modal-render
    poll off the wire under fast clicks.
    """
    import time

    import httpx

    from backend.app.services.slicer_routing import resolve_sidecar_url, slicer_label

    chosen, api_url = await resolve_sidecar_url(db, slicer_override=slicer)
    if chosen is None:
        raise HTTPException(status_code=400, detail=f"Unknown slicer: '{slicer}'.")
    if not api_url:
        return {"healthy": False, "error": f"{slicer_label(chosen)} API URL is not configured.", "url": None}

    cache_key = (chosen, api_url)
    now = time.monotonic()
    cached = _health_cache.get(cache_key)
    if cached and (now - cached[0]) < _HEALTH_TTL_SECONDS:
        return cached[1]

    payload: dict
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{api_url}/health")
        if response.status_code >= 200 and response.status_code < 300:
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            version = body.get("version") or body.get("checks", {}).get("orcaslicer", {}).get("version")
            payload = {"healthy": True, "url": api_url, "version": version}
        else:
            payload = {"healthy": False, "url": api_url, "error": f"sidecar returned HTTP {response.status_code}"}
    except httpx.RequestError as exc:
        payload = {"healthy": False, "url": api_url, "error": f"sidecar unreachable: {type(exc).__name__}"}

    _health_cache[cache_key] = (now, payload)
    return payload


@router.get("/preview-progress/{request_id}")
async def get_preview_slice_progress(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Proxy to the sidecar's ``GET /slice/progress/:requestId``.

    The SliceModal's filament-requirements call kicks off a real preview
    slice on the sidecar to discover which AMS slots the picked plate
    actually consumes. That HTTP call holds open for the full slice
    duration (multi-second to multi-minute on complex models), and the
    browser can't reach the sidecar directly thanks to the same-origin
    policy + the sidecar's CORS allowlist. This endpoint forwards the
    poll so the modal's inline spinner can show "Generating G-code (45%)"
    instead of an opaque elapsed-time counter while the preview runs.

    Returns the sidecar's snapshot verbatim, or 404 when the request_id
    is unknown / completed and grace-window-expired.
    """
    import httpx

    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        raise HTTPException(status_code=503, detail="No slicer sidecar configured")
    url = f"{api_url}/slice/progress/{request_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
    except httpx.RequestError:
        # Sidecar unreachable: surface as 503 instead of 500 so the
        # frontend's poller can keep trying without flagging a hard error.
        raise HTTPException(status_code=503, detail="Slicer sidecar unreachable") from None
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Progress unavailable")
    return response.json()


# ---------------------------------------------------------------------------
# Slicer Preset Bundles (.bbscfg) — pick presets from a stored bundle
# instead of resolving cloud/local/standard PresetRefs every slice.
# ---------------------------------------------------------------------------


def _bundle_summary_to_dict(bundle: BundleSummary) -> dict:
    """Serialise a BundleSummary for the JSON response."""
    return {
        "id": bundle.id,
        "printer_preset_name": bundle.printer_preset_name,
        "printer": bundle.printer,
        "process": bundle.process,
        "filament": bundle.filament,
        "version": bundle.version,
    }


def _map_sidecar_error_to_http(exc: SlicerApiError) -> HTTPException:
    """Sidecar 4xx → 400, 5xx → 502, unreachable → 503, BundleNotFound → 404.

    Keeps the frontend's error rendering uniform across all bundle routes —
    a sidecar that's misconfigured / offline shows a clear 503 instead of
    leaking the underlying connection error to the operator.
    """
    if isinstance(exc, BundleNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SlicerInputError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, SlicerApiUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, SlicerApiServerError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/bundles", status_code=201)
async def import_slicer_bundle(
    file: UploadFile = File(...),  # noqa: B008 — FastAPI Depends-style default
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
) -> dict:
    """``POST /slicer/bundles`` — upload a BambuStudio Printer Preset Bundle (.bbscfg).

    Idempotent on the sidecar side: re-uploading the same file yields the
    same id. Sidecar 4xx → 400 (invalid .bbscfg / path-traversal /
    manifest validation failure), 5xx → 502, unreachable → 503.
    """
    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        raise HTTPException(status_code=503, detail="No slicer sidecar configured")
    zip_bytes = await file.read()
    filename = file.filename or "bundle.bbscfg"
    try:
        async with SlicerApiService(base_url=api_url) as svc:
            bundle = await svc.import_bundle(zip_bytes, filename=filename)
    except SlicerApiError as exc:
        # Log the sidecar's actual reject reason at WARNING. The FE-only
        # toast leaves us blind during triage — the access log only carries
        # the bare status code, and a 400 / 502 / 503 from this path is
        # always unexpected (non-bbscfg upload, sidecar disk write failure,
        # sidecar offline, …). Logging here means the next reporter's
        # support bundle contains the answer (upstream Bambuddy #1312).
        if isinstance(exc, SlicerInputError):
            logger.warning(
                "Bundle import rejected by sidecar (%s, %d bytes): %s",
                filename,
                len(zip_bytes),
                exc,
            )
        elif isinstance(exc, SlicerApiUnavailableError):
            logger.warning("Bundle import: sidecar unreachable (%s): %s", api_url, exc)
        else:
            logger.warning(
                "Bundle import: sidecar error (%s, %d bytes): %s",
                filename,
                len(zip_bytes),
                exc,
            )
        raise _map_sidecar_error_to_http(exc) from exc
    return _bundle_summary_to_dict(bundle)


@router.get("/bundles")
async def list_slicer_bundles(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
) -> list[dict]:
    """``GET /slicer/bundles`` — every imported bundle and its presets.

    Returns ``[]`` when the sidecar's bundle store is empty. 503 when the
    sidecar is unreachable.
    """
    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        # Empty list rather than 503 here so the SliceModal's "is the bundle
        # picker visible?" decision degrades gracefully on installs where
        # the sidecar isn't configured at all.
        return []
    try:
        async with SlicerApiService(base_url=api_url) as svc:
            bundles = await svc.list_bundles()
    except SlicerApiError as exc:
        raise _map_sidecar_error_to_http(exc) from exc
    return [_bundle_summary_to_dict(b) for b in bundles]


@router.get("/bundles/{bundle_id}")
async def get_slicer_bundle(
    bundle_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
) -> dict:
    """``GET /slicer/bundles/<id>`` — single bundle summary."""
    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        raise HTTPException(status_code=503, detail="No slicer sidecar configured")
    try:
        async with SlicerApiService(base_url=api_url) as svc:
            bundle = await svc.get_bundle(bundle_id)
    except SlicerApiError as exc:
        raise _map_sidecar_error_to_http(exc) from exc
    return _bundle_summary_to_dict(bundle)


@router.delete("/bundles/{bundle_id}", status_code=204)
async def delete_slicer_bundle(
    bundle_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
) -> None:
    """``DELETE /slicer/bundles/<id>`` — remove a stored bundle."""
    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        raise HTTPException(status_code=503, detail="No slicer sidecar configured")
    try:
        async with SlicerApiService(base_url=api_url) as svc:
            await svc.delete_bundle(bundle_id)
    except SlicerApiError as exc:
        raise _map_sidecar_error_to_http(exc) from exc
