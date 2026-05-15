"""
Bambu Lab Cloud API Routes

Handles authentication and profile management with Bambu Cloud.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequirePermission, _validate_api_key, security
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.cloud import (
    CloudAuthStatus,
    CloudDevice,
    CloudLoginRequest,
    CloudLoginResponse,
    CloudTokenRequest,
    CloudVerifyRequest,
    FirmwareUpdateInfo,
    FirmwareUpdatesResponse,
    SlicerSetting,
    SlicerSettingCreate,
    SlicerSettingDeleteResponse,
    SlicerSettingsResponse,
    SlicerSettingUpdate,
)
from backend.app.services.bambu_cloud import (
    _SLICER_API_VERSION,
    BambuCloudAuthError,
    BambuCloudError,
    BambuCloudService,
)
from backend.app.utils.filament_ids import filament_id_to_setting_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cloud", tags=["cloud"])


# Keys for storing cloud credentials in settings
CLOUD_TOKEN_KEY = "bambu_cloud_token"
CLOUD_EMAIL_KEY = "bambu_cloud_email"
CLOUD_REGION_KEY = "bambu_cloud_region"


def _normalise_region(region: str | None) -> str:
    """Treat NULL/empty/unknown as 'global' for legacy rows that predate the region column."""
    return region if region in ("global", "china") else "global"


async def get_stored_token(db: AsyncSession, user: User | None = None) -> tuple[str | None, str | None, str]:
    """Get stored cloud token, email, and region.

    When a user is provided (auth enabled), returns that user's per-user credentials.
    When user is None (auth disabled), falls back to global Settings table.
    Region defaults to ``"global"`` when unset (including for rows that predate
    the ``cloud_region`` column).
    """
    if user is not None:
        return user.cloud_token, user.cloud_email, _normalise_region(user.cloud_region)

    # Fallback: global storage (auth disabled)
    result = await db.execute(
        select(Settings).where(Settings.key.in_([CLOUD_TOKEN_KEY, CLOUD_EMAIL_KEY, CLOUD_REGION_KEY]))
    )
    settings = {s.key: s.value for s in result.scalars().all()}
    return (
        settings.get(CLOUD_TOKEN_KEY),
        settings.get(CLOUD_EMAIL_KEY),
        _normalise_region(settings.get(CLOUD_REGION_KEY)),
    )


async def store_token(db: AsyncSession, token: str, email: str, region: str, user: User | None = None) -> None:
    """Store cloud token, email, and region.

    When a user is provided (auth enabled), stores on the user record.
    When user is None (auth disabled), stores in global Settings table.
    """
    region = _normalise_region(region)
    if user is not None:
        # User object is from the auth dependency's session (detached),
        # so use a direct UPDATE via the route's db session.
        from sqlalchemy import update

        await db.execute(
            update(User).where(User.id == user.id).values(cloud_token=token, cloud_email=email, cloud_region=region)
        )
        await db.commit()
        return

    # Fallback: global storage (auth disabled)
    for key, value in [(CLOUD_TOKEN_KEY, token), (CLOUD_EMAIL_KEY, email), (CLOUD_REGION_KEY, region)]:
        result = await db.execute(select(Settings).where(Settings.key == key))
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = value
        else:
            db.add(Settings(key=key, value=value))
    await db.commit()


async def clear_token(db: AsyncSession, user: User | None = None) -> None:
    """Clear stored cloud token, email, and region.

    When a user is provided (auth enabled), clears that user's credentials.
    When user is None (auth disabled), clears from global Settings table.
    """
    if user is not None:
        from sqlalchemy import update

        await db.execute(
            update(User).where(User.id == user.id).values(cloud_token=None, cloud_email=None, cloud_region=None)
        )
        await db.commit()
        return

    # Fallback: global storage (auth disabled)
    result = await db.execute(
        select(Settings).where(Settings.key.in_([CLOUD_TOKEN_KEY, CLOUD_EMAIL_KEY, CLOUD_REGION_KEY]))
    )
    for setting in result.scalars().all():
        await db.delete(setting)
    await db.commit()


async def build_authenticated_cloud(db: AsyncSession, user: User | None) -> BambuCloudService | None:
    """Build a per-request cloud service seeded with the caller's stored token + region.

    Returns ``None`` when no token is stored, so callers can 401 without
    constructing (and then closing) a useless client. Caller is responsible
    for ``await cloud.close()``.
    """
    token, _email, region = await get_stored_token(db, user)
    if not token:
        return None
    cloud = BambuCloudService(region=region)
    cloud.set_token(token)
    return cloud


async def resolve_api_key_cloud_owner(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Permissive dep: resolve "the user behind this API key" for cloud routes.

    Returns the owning ``User`` when:

    1. The request carries a valid, enabled API key (X-API-Key header or
       Bearer bb_xxx).
    2. The key has ``can_access_cloud=True`` AND a non-NULL ``user_id``.
    3. That user exists and is active.

    Returns ``None`` for every other case (no auth header, JWT, key without
    cloud access, key without owner, missing/inactive owner). Designed to
    be combined with the route's existing ``current_user`` dependency:

        cloud_token_user = current_user or api_key_cloud_owner

    This way a JWT-authenticated request keeps using its own token (the old
    behaviour), an API-key request gets routed to the key's owner's token
    (the new #1182 behaviour), and an ownerless / cloud-denied API key
    silently falls through so the route can 401 like before.

    Permissive on purpose — never raises. ``RequirePermission`` already
    guards the actual route, so a bad key surfaces there with the correct
    403/401, not from inside this resolver.
    """
    api_key_value: str | None = None
    if x_api_key:
        api_key_value = x_api_key
    elif credentials is not None and credentials.credentials.startswith("bb_"):
        api_key_value = credentials.credentials

    if not api_key_value:
        return None

    api_key = await _validate_api_key(db, api_key_value)
    if api_key is None or not api_key.can_access_cloud or api_key.user_id is None:
        return None

    # Load the user with groups so downstream permission checks (if the
    # caller composes this with anything that inspects permissions) work
    # without lazy-loading.
    result = await db.execute(
        select(User).where(User.id == api_key.user_id, User.is_active.is_(True)).options(selectinload(User.groups))
    )
    return result.scalar_one_or_none()


@router.get("/status", response_model=CloudAuthStatus)
async def get_auth_status(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """Get current cloud authentication status.

    Reads the stored credentials in one DB round-trip. ``region`` is exposed
    so the frontend can show "Connected (China)" after a reload without
    relying on local state.
    """
    token, email, region = await get_stored_token(db, current_user)
    if not token:
        return CloudAuthStatus(is_authenticated=False, email=None, region=None)

    cloud = BambuCloudService(region=region)
    cloud.set_token(token)
    try:
        authenticated = cloud.is_authenticated
        return CloudAuthStatus(
            is_authenticated=authenticated,
            email=email if authenticated else None,
            region=region if authenticated else None,
        )
    finally:
        await cloud.close()


@router.post("/login", response_model=CloudLoginResponse)
async def login(
    request: CloudLoginRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Initiate login to Bambu Cloud.

    This will trigger either:
    - Email verification: A code is sent to the user's email
    - TOTP verification: User enters code from their authenticator app

    After receiving/generating the code, call /cloud/verify to complete the login.
    For TOTP, include the tfa_key from this response in the verify request.
    """
    cloud = BambuCloudService(region=request.region)

    try:
        result = await cloud.login_request(request.email, request.password)

        if result.get("success") and cloud.access_token:
            # Direct login succeeded (rare)
            await store_token(db, cloud.access_token, request.email, request.region, current_user)

        return CloudLoginResponse(
            success=result.get("success", False),
            needs_verification=result.get("needs_verification", False),
            message=result.get("message", "Unknown error"),
            verification_type=result.get("verification_type"),
            tfa_key=result.get("tfa_key"),
        )
    except BambuCloudAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


@router.post("/verify", response_model=CloudLoginResponse)
async def verify_code(
    request: CloudVerifyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Complete login with verification code (email or TOTP).

    For email verification:
    - After calling /cloud/login, the user receives an email with a 6-digit code
    - Submit the code with email address

    For TOTP verification:
    - The user enters the 6-digit code from their authenticator app
    - Include the tfa_key from the /cloud/login response

    ``request.region`` must match the region used in /cloud/login so that
    the TOTP call hits the correct TFA endpoint (bambulab.com vs bambulab.cn).
    """
    cloud = BambuCloudService(region=request.region)

    try:
        # Use TOTP verification if tfa_key is provided
        if request.tfa_key:
            result = await cloud.verify_totp(request.tfa_key, request.code)
        else:
            result = await cloud.verify_code(request.email, request.code)

        if result.get("success") and cloud.access_token:
            await store_token(db, cloud.access_token, request.email, request.region, current_user)

        return CloudLoginResponse(
            success=result.get("success", False),
            needs_verification=False,
            message=result.get("message", "Unknown error"),
        )
    except BambuCloudAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


@router.post("/token", response_model=CloudAuthStatus)
async def set_token(
    request: CloudTokenRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Set access token directly.

    For users who already have a token (e.g., from Bambu Studio). The
    selected ``region`` is persisted alongside the token so every subsequent
    request hits the right Bambu API endpoint, including after a restart.
    """
    cloud = BambuCloudService(region=request.region)
    cloud.set_token(request.access_token)

    try:
        # Verify token works by trying to get profile
        await cloud.get_user_profile()
        await store_token(db, request.access_token, "token-auth", request.region, current_user)
        return CloudAuthStatus(is_authenticated=True, email="token-auth", region=request.region)
    except BambuCloudError:
        raise HTTPException(status_code=401, detail="Invalid token")
    finally:
        await cloud.close()


@router.post("/logout")
async def logout(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """Log out of Bambu Cloud."""
    await clear_token(db, current_user)
    return {"success": True}


@router.get("/settings", response_model=SlicerSettingsResponse)
async def get_slicer_settings(
    version: str = _SLICER_API_VERSION,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Get all slicer settings (filament, printer, process presets).

    Requires authentication.
    """
    cloud = await build_authenticated_cloud(db, current_user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        data = await cloud.get_slicer_settings(version)

        result = SlicerSettingsResponse()

        # Map API keys to our types (API uses 'print' for process presets)
        type_mapping = {
            "filament": "filament",
            "printer": "printer",
            "print": "process",  # API calls it 'print', we call it 'process'
        }

        for api_key, our_type in type_mapping.items():
            type_data = data.get(api_key, {})
            private_settings = type_data.get("private", [])
            public_settings = type_data.get("public", [])

            parsed = []
            # Private (custom) presets first
            for s in private_settings:
                parsed.append(
                    SlicerSetting(
                        setting_id=s.get("setting_id", s.get("id", "")),
                        name=s.get("name", "Unknown"),
                        type=our_type,
                        version=s.get("version"),
                        user_id=s.get("user_id"),
                        updated_time=s.get("updated_time"),
                        is_custom=True,
                    )
                )
            # Public (default) presets
            for s in public_settings:
                parsed.append(
                    SlicerSetting(
                        setting_id=s.get("setting_id", s.get("id", "")),
                        name=s.get("name", "Unknown"),
                        type=our_type,
                        version=s.get("version"),
                        user_id=s.get("user_id"),
                        updated_time=s.get("updated_time"),
                        is_custom=False,
                    )
                )
            setattr(result, our_type, parsed)

        return result
    except BambuCloudAuthError:
        await clear_token(db, current_user)
        raise HTTPException(status_code=401, detail="Authentication expired")
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


@router.get("/settings/{setting_id}")
async def get_setting_detail(
    setting_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Get detailed information for a specific setting/preset.

    Returns the full preset configuration.
    """
    cloud = await build_authenticated_cloud(db, current_user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        data = await cloud.get_setting_detail(setting_id)
        return data
    except BambuCloudAuthError:
        await clear_token(db, current_user)
        raise HTTPException(status_code=401, detail="Authentication expired")
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


@router.get("/filaments", response_model=list[SlicerSetting])
async def get_filament_presets(
    version: str = _SLICER_API_VERSION,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """
    Get just filament presets (convenience endpoint).

    Returns all filament presets with custom presets first.
    Uses the same cache as get_slicer_settings.
    """
    settings = await get_slicer_settings(version=version, db=db, current_user=current_user)
    return settings.filament


# Cache for filament preset info (setting_id -> {name, k})
_filament_cache: dict[str, dict] = {}
_filament_cache_time: float = 0
FILAMENT_CACHE_TTL = 300  # 5 minutes

# Built-in filament ID → name mapping (fallback when cloud API and local profiles
# don't have the entry). Based on Bambu Lab's known filament catalogue.
_BUILTIN_FILAMENT_NAMES: dict[str, str] = {
    "GFA00": "Bambu PLA Basic",
    "GFA01": "Bambu PLA Matte",
    "GFA02": "Bambu PLA Metal",
    "GFA05": "Bambu PLA Silk",
    "GFA06": "Bambu PLA Silk+",
    "GFA07": "Bambu PLA Marble",
    "GFA08": "Bambu PLA Sparkle",
    "GFA09": "Bambu PLA Tough",
    "GFA11": "Bambu PLA Aero",
    "GFA12": "Bambu PLA Glow",
    "GFA13": "Bambu PLA Dynamic",
    "GFA15": "Bambu PLA Galaxy",
    "GFA16": "Bambu PLA Wood",
    "GFA50": "Bambu PLA-CF",
    "GFB00": "Bambu ABS",
    "GFB01": "Bambu ASA",
    "GFB02": "Bambu ASA-Aero",
    "GFB50": "Bambu ABS-GF",
    "GFB51": "Bambu ASA-CF",
    "GFB60": "PolyLite ABS",
    "GFB61": "PolyLite ASA",
    "GFB98": "Generic ASA",
    "GFB99": "Generic ABS",
    "GFC00": "Bambu PC",
    "GFC01": "Bambu PC FR",
    "GFC99": "Generic PC",
    "GFG00": "Bambu PETG Basic",
    "GFG01": "Bambu PETG Translucent",
    "GFG02": "Bambu PETG HF",
    "GFG50": "Bambu PETG-CF",
    "GFG60": "PolyLite PETG",
    "GFG96": "Generic PETG HF",
    "GFG97": "Generic PCTG",
    "GFG98": "Generic PETG-CF",
    "GFG99": "Generic PETG",
    "GFL00": "PolyLite PLA",
    "GFL01": "PolyTerra PLA",
    "GFL03": "eSUN PLA+",
    "GFL04": "Overture PLA",
    "GFL05": "Overture Matte PLA",
    "GFL06": "Fiberon PETG-ESD",
    "GFL50": "Fiberon PA6-CF",
    "GFL51": "Fiberon PA6-GF",
    "GFL52": "Fiberon PA12-CF",
    "GFL53": "Fiberon PA612-CF",
    "GFL54": "Fiberon PET-CF",
    "GFL55": "Fiberon PETG-rCF",
    "GFL95": "Generic PLA High Speed",
    "GFL96": "Generic PLA Silk",
    "GFL98": "Generic PLA-CF",
    "GFL99": "Generic PLA",
    "GFN03": "Bambu PA-CF",
    "GFN04": "Bambu PAHT-CF",
    "GFN05": "Bambu PA6-CF",
    "GFN06": "Bambu PPA-CF",
    "GFN08": "Bambu PA6-GF",
    "GFN96": "Generic PPA-GF",
    "GFN97": "Generic PPA-CF",
    "GFN98": "Generic PA-CF",
    "GFN99": "Generic PA",
    "GFP95": "Generic PP-GF",
    "GFP96": "Generic PP-CF",
    "GFP97": "Generic PP",
    "GFP98": "Generic PE-CF",
    "GFP99": "Generic PE",
    "GFR98": "Generic PHA",
    "GFR99": "Generic EVA",
    "GFS00": "Bambu Support W",
    "GFS01": "Bambu Support G",
    "GFS02": "Bambu Support For PLA",
    "GFS03": "Bambu Support For PA/PET",
    "GFS04": "Bambu PVA",
    "GFS05": "Bambu Support For PLA/PETG",
    "GFS06": "Bambu Support for ABS",
    "GFS97": "Generic BVOH",
    "GFS98": "Generic HIPS",
    "GFS99": "Generic PVA",
    "GFT01": "Bambu PET-CF",
    "GFT02": "Bambu PPS-CF",
    "GFT97": "Generic PPS",
    "GFT98": "Generic PPS-CF",
    "GFU00": "Bambu TPU 95A HF",
    "GFU01": "Bambu TPU 95A",
    "GFU02": "Bambu TPU for AMS",
    "GFU98": "Generic TPU for AMS",
    "GFU99": "Generic TPU",
}


async def _enrich_from_local_presets(
    unresolved_ids: list[str],
    result: dict,
    db: AsyncSession,
) -> dict:
    """Fall back to local profiles for filament IDs not resolved by cloud.

    Matches by checking the setting_id field inside the local preset's
    resolved JSON blob (stored in the 'setting' column).
    """
    from sqlalchemy import text

    from backend.app.models.local_preset import LocalPreset

    # Build lookup: converted setting_id -> original filament_id
    id_map: dict[str, str] = {}
    for fid in unresolved_ids:
        converted = _filament_id_to_setting_id(fid)
        id_map[converted] = fid
        # Also map the original in case the JSON uses that form
        id_map[fid] = fid

    try:
        # Query filament presets that have a setting_id matching any of our IDs
        # json_extract is supported in SQLite >= 3.9 and all modern Python builds
        candidates = await db.execute(
            select(LocalPreset).where(
                LocalPreset.preset_type == "filament",
                text("json_extract(setting, '$.setting_id') IS NOT NULL"),
            )
        )
        for preset in candidates.scalars().all():
            try:
                setting_data = json.loads(preset.setting) if isinstance(preset.setting, str) else preset.setting
                preset_setting_id = setting_data.get("setting_id", "")
                if preset_setting_id in id_map:
                    original_id = id_map[preset_setting_id]
                    info = {"name": preset.name, "k": None}
                    # Try to extract K value from the local preset
                    pa = setting_data.get("pressure_advance")
                    if pa is not None:
                        try:
                            k_val = float(pa[0]) if isinstance(pa, list) else float(pa)
                            info["k"] = k_val
                        except (ValueError, TypeError, IndexError):
                            pass
                    _filament_cache[original_id] = info
                    result[original_id] = info
            except Exception:
                continue
    except Exception as e:
        logger.warning("Failed to search local presets for filament info: %s", e)

    # Phase 4: Fall back to built-in filament name table for any still without a name
    for fid in unresolved_ids:
        if fid not in result or not result[fid].get("name"):
            name = _BUILTIN_FILAMENT_NAMES.get(fid, "")
            if name:
                # Preserve K value from earlier phases if available
                existing_k = result.get(fid, {}).get("k")
                info = {"name": name, "k": existing_k}
                _filament_cache[fid] = info
                result[fid] = info

    # Fill remaining unresolved with empty entries
    for fid in unresolved_ids:
        if fid not in result:
            _filament_cache[fid] = {"name": "", "k": None}
            result[fid] = {"name": "", "k": None}

    return result


# _filament_id_to_setting_id is now imported from backend.app.utils.filament_ids
_filament_id_to_setting_id = filament_id_to_setting_id


@router.post("/filament-info")
async def get_filament_info(
    setting_ids: list[str] = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """
    Get filament preset info (name and K value) for multiple setting IDs.

    Used to enrich AMS tray and nozzle rack tooltips with preset data.
    Lookup order: cache → cloud → local profiles → built-in table → empty fallback.
    """
    import time

    logger.info("get_filament_info called with %s IDs: %s", len(setting_ids), setting_ids)

    global _filament_cache, _filament_cache_time

    # Clear stale cache
    if time.time() - _filament_cache_time > FILAMENT_CACHE_TTL:
        _filament_cache = {}
        _filament_cache_time = time.time()

    result = {}
    unresolved_ids: list[str] = []

    # Phase 1: Check cache
    for setting_id in setting_ids:
        if not setting_id:
            continue
        if setting_id in _filament_cache:
            result[setting_id] = _filament_cache[setting_id]
        else:
            unresolved_ids.append(setting_id)

    # Phase 2: Try cloud for uncached IDs
    if unresolved_ids:
        cloud = await build_authenticated_cloud(db, current_user)
        if cloud is not None and cloud.is_authenticated:
            try:
                still_unresolved: list[str] = []
                for setting_id in unresolved_ids:
                    try:
                        api_setting_id = _filament_id_to_setting_id(setting_id)
                        data = await cloud.get_setting_detail(api_setting_id)
                        setting = data.get("setting", {})
                        name = data.get("name", "")
                        k_value = setting.get("pressure_advance")
                        if k_value is not None:
                            try:
                                k_value = float(k_value)
                            except (ValueError, TypeError):
                                k_value = None

                        info: dict = {"name": name, "k": k_value}

                        # Derive bed / nozzle / max-volumetric-speed from the
                        # cloud preset content so the calibration wizard's
                        # production-mode form can auto-fill those values
                        # (operator picks a filament preset → BS-shape
                        # temperatures + speed appear without manual entry).
                        # Bambu's preset JSON stores these as
                        # ConfigOptionFloatsNullable / ConfigOptionInts —
                        # list-valued, one entry per logical filament slot;
                        # we surface the first non-null entry as the
                        # canonical value for the calibration's single-slot
                        # use case. The cloud also exposes these as
                        # string-typed values for back-compat with older
                        # GUIs; cast best-effort.
                        def _first_numeric(value):
                            # ``"nil"`` is Bambu's serialization sentinel for
                            # nullable ConfigOption entries (BS Config.hpp
                            # ``"nil"`` literal — "inherit from parent").
                            # Treat it the same as None / "" so the caller
                            # falls through to parent inheritance lookup.
                            if value is None or value == "nil":
                                return None
                            if isinstance(value, list):
                                for entry in value:
                                    if entry is None or entry == "" or entry == "nil":
                                        continue
                                    try:
                                        return float(entry)
                                    except (ValueError, TypeError):
                                        continue
                                return None
                            try:
                                return float(value)
                            except (ValueError, TypeError):
                                return None

                        # Bambu serializes nullable ConfigOption fields with
                        # the literal string ``"nil"`` as a sentinel for
                        # "inherit from parent" — see BS
                        # ``ConfigOptionVectorBase`` in Config.hpp (search
                        # `"nil"`). Cloud presets returned by
                        # ``get_setting_detail`` carry only the delta
                        # against ``base_id``; fields the operator left at
                        # parent defaults come back as ``["nil"]`` (or
                        # plain absent). Bed temp + max-vol-speed worked
                        # because they're usually filament-specific
                        # overrides; ``nozzle_temperature`` is more often
                        # inherited from the base material preset (e.g.
                        # ``Generic PETG-HF @BBL A1M`` keeps temps from
                        # ``fdm_filament_pet`` parent).
                        #
                        # Walk the ``base_id`` chain until a non-nil value
                        # surfaces. Each hop hits the cloud, so cap depth
                        # at 5 (BS inheritance chains are at most 2-3 hops
                        # in practice).
                        async def _resolve_inherited_numeric(
                            initial_setting: dict, initial_base_id: str | None, key: str, max_depth: int = 5
                        ):
                            """Walk the cloud preset inheritance chain to
                            find a non-nil numeric value for ``key``.
                            Stops at first concrete value, ``base_id ==
                            None``, depth cap, or fetch failure."""
                            val = _first_numeric(initial_setting.get(key))
                            if val is not None:
                                return val
                            cur_base = initial_base_id
                            for _ in range(max_depth):
                                if not cur_base:
                                    return None
                                try:
                                    parent_data = await cloud.get_setting_detail(cur_base)
                                except Exception as e:
                                    logger.debug(
                                        "get_filament_info: parent fetch failed for %s: %s",
                                        cur_base,
                                        e,
                                    )
                                    return None
                                parent_setting = parent_data.get("setting", {}) or {}
                                val = _first_numeric(parent_setting.get(key))
                                if val is not None:
                                    return val
                                cur_base = parent_data.get("base_id") or parent_setting.get("inherits")
                            return None

                        base_id = data.get("base_id") or setting.get("inherits")
                        nozzle_temp = await _resolve_inherited_numeric(setting, base_id, "nozzle_temperature")
                        bed_temp = _first_numeric(setting.get("hot_plate_temp") or setting.get("bed_temperature"))
                        if bed_temp is None:
                            bed_temp = await _resolve_inherited_numeric(setting, base_id, "hot_plate_temp")
                        max_vol_speed = _first_numeric(setting.get("filament_max_volumetric_speed"))
                        if max_vol_speed is None:
                            max_vol_speed = await _resolve_inherited_numeric(
                                setting, base_id, "filament_max_volumetric_speed"
                            )
                        if nozzle_temp is not None:
                            info["nozzle_temperature"] = nozzle_temp
                        else:
                            temp_keys = sorted(k for k in setting if "temp" in k.lower() or "nozzle" in k.lower())
                            logger.warning(
                                "get_filament_info: setting_id=%s nozzle_temperature unresolved "
                                "(base_id=%s); local temp/nozzle keys: %s",
                                setting_id,
                                base_id,
                                temp_keys,
                            )
                        if bed_temp is not None:
                            info["hot_plate_temp"] = bed_temp
                        if max_vol_speed is not None:
                            info["filament_max_volumetric_speed"] = max_vol_speed
                        _filament_cache[setting_id] = info
                        result[setting_id] = info

                        if not name:
                            still_unresolved.append(setting_id)
                    except Exception as e:
                        logger.warning(
                            f"Failed to get cloud preset {setting_id} "
                            f"(API ID: {_filament_id_to_setting_id(setting_id)}): {e}"
                        )
                        still_unresolved.append(setting_id)

                unresolved_ids = still_unresolved
            finally:
                await cloud.close()
        elif cloud is not None:
            await cloud.close()

    # Phase 3: Try local profiles for any IDs still without a name
    if unresolved_ids:
        result = await _enrich_from_local_presets(unresolved_ids, result, db)

    return result


@router.get("/devices", response_model=list[CloudDevice])
async def get_devices(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.PRINTERS_READ),
):
    """
    Get list of bound printer devices.

    Returns printers registered to the user's Bambu account.
    """
    cloud = await build_authenticated_cloud(db, current_user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        data = await cloud.get_devices()
        devices = data.get("devices", [])

        return [
            CloudDevice(
                dev_id=d.get("dev_id", ""),
                name=d.get("name", "Unknown"),
                dev_model_name=d.get("dev_model_name"),
                dev_product_name=d.get("dev_product_name"),
                online=d.get("online", False),
            )
            for d in devices
        ]
    except BambuCloudAuthError:
        await clear_token(db, current_user)
        raise HTTPException(status_code=401, detail="Authentication expired")
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


@router.get("/firmware-updates", response_model=FirmwareUpdatesResponse)
async def get_firmware_updates(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.FIRMWARE_READ),
):
    """
    Check for firmware updates for all bound devices.

    Returns firmware version info for each device including:
    - Current installed version
    - Latest available version
    - Whether an update is available
    - Release notes for the latest version

    Requires cloud authentication.
    """
    cloud = await build_authenticated_cloud(db, current_user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        # First get list of bound devices
        devices_data = await cloud.get_devices()
        devices = devices_data.get("devices", [])

        updates = []
        updates_available = 0

        # Check firmware for each device
        for device in devices:
            device_id = device.get("dev_id", "")
            device_name = device.get("name", "Unknown")

            try:
                firmware_info = await cloud.get_firmware_version(device_id)
                update_available = firmware_info.get("update_available", False)

                if update_available:
                    updates_available += 1

                updates.append(
                    FirmwareUpdateInfo(
                        device_id=device_id,
                        device_name=device_name,
                        current_version=firmware_info.get("current_version"),
                        latest_version=firmware_info.get("latest_version"),
                        update_available=update_available,
                        release_notes=firmware_info.get("release_notes"),
                    )
                )
            except BambuCloudError as e:
                logger.warning("Failed to get firmware info for %s: %s", device_name, e)
                # Still include device but with unknown firmware status
                updates.append(
                    FirmwareUpdateInfo(
                        device_id=device_id,
                        device_name=device_name,
                        current_version=None,
                        latest_version=None,
                        update_available=False,
                        release_notes=None,
                    )
                )

        return FirmwareUpdatesResponse(updates=updates, updates_available=updates_available)

    except BambuCloudAuthError:
        await clear_token(db, current_user)
        raise HTTPException(status_code=401, detail="Authentication expired")
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


@router.post("/settings")
async def create_setting(
    request: SlicerSettingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Create a new slicer preset/setting.

    Creates a new preset on Bambu Cloud. The preset inherits from a base preset
    and only stores the delta (modified values).

    Type should be: 'filament', 'print', or 'printer'
    """
    cloud = await build_authenticated_cloud(db, current_user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        data = await cloud.create_setting(
            preset_type=request.type,
            name=request.name,
            base_id=request.base_id,
            setting=request.setting,
            version=request.version,
        )
        return data
    except BambuCloudAuthError:
        await clear_token(db, current_user)
        raise HTTPException(status_code=401, detail="Authentication expired")
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


@router.put("/settings/{setting_id}")
async def update_setting(
    setting_id: str,
    request: SlicerSettingUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Update an existing slicer preset/setting.

    Updates the preset's name and/or settings on Bambu Cloud.
    """
    cloud = await build_authenticated_cloud(db, current_user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        data = await cloud.update_setting(
            setting_id=setting_id,
            name=request.name,
            setting=request.setting,
        )
        return data
    except BambuCloudAuthError:
        await clear_token(db, current_user)
        raise HTTPException(status_code=401, detail="Authentication expired")
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


@router.delete("/settings/{setting_id}", response_model=SlicerSettingDeleteResponse)
async def delete_setting(
    setting_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Delete a slicer preset/setting.

    Removes the preset from Bambu Cloud. This cannot be undone.
    """
    cloud = await build_authenticated_cloud(db, current_user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        result = await cloud.delete_setting(setting_id)
        return SlicerSettingDeleteResponse(
            success=result.get("success", True),
            message=result.get("message", "Setting deleted"),
        )
    except BambuCloudAuthError:
        await clear_token(db, current_user)
        raise HTTPException(status_code=401, detail="Authentication expired")
    except BambuCloudError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await cloud.close()


# Path to field definition files
FIELDS_DATA_DIR = Path(__file__).parent.parent.parent / "data"

# Cache for field definitions (loaded once)
_fields_cache: dict[str, dict] = {}


def _load_fields(preset_type: str) -> dict:
    """Load field definitions from JSON file."""
    if preset_type in _fields_cache:
        return _fields_cache[preset_type]

    # Map API type names to file names
    file_map = {
        "filament": "filament_fields.json",
        "print": "process_fields.json",
        "process": "process_fields.json",
        "printer": "printer_fields.json",
    }

    filename = file_map.get(preset_type)
    if not filename:
        raise HTTPException(status_code=400, detail=f"Unknown preset type: {preset_type}")

    file_path = FIELDS_DATA_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Field definitions not found for: {preset_type}")

    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)

    _fields_cache[preset_type] = data
    return data


@router.get("/builtin-filaments")
async def get_builtin_filaments(
    _: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """
    Get built-in filament names as a fallback source.

    Returns the static _BUILTIN_FILAMENT_NAMES table as a list of
    {filament_id, name} objects.  Used by the frontend when cloud
    and local profiles are unavailable.
    """
    return [{"filament_id": fid, "name": name} for fid, name in _BUILTIN_FILAMENT_NAMES.items()]


# Cache for filament_id → name mapping (resolved from cloud preset details)
_filament_id_name_cache: dict[str, str] = {}
_filament_id_name_cache_time: float = 0


@router.get("/filament-id-map")
async def get_filament_id_map(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.INVENTORY_READ),
):
    """
    Get filament_id → name mapping for user cloud presets.

    K-profiles store a filament_id (e.g., "P4d64437") which is different from
    the cloud preset setting_id (e.g., "PFUS9ac902733670a9"). This endpoint
    fetches details for all custom presets and returns the mapping.
    Cached for 5 minutes.
    """
    import time

    global _filament_id_name_cache, _filament_id_name_cache_time

    if _filament_id_name_cache and time.time() - _filament_id_name_cache_time < FILAMENT_CACHE_TTL:
        return _filament_id_name_cache

    cloud = await build_authenticated_cloud(db, current_user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        return _filament_id_name_cache or {}

    try:
        data = await cloud.get_slicer_settings()
        custom_presets = data.get("filament", {}).get("private", [])

        result: dict[str, str] = {}
        for preset in custom_presets:
            setting_id = preset.get("setting_id", "")
            if not setting_id:
                continue
            try:
                detail = await cloud.get_setting_detail(setting_id)
                fid = detail.get("filament_id", "")
                name = detail.get("name", "")
                if fid and name:
                    # Strip printer/nozzle suffix: "Devil Design PLA Basic @Bambu Lab H2D 0.4 nozzle" → "Devil Design PLA Basic"
                    clean_name = name.split(" @")[0].strip() if " @" in name else name
                    result[fid] = clean_name
            except Exception:
                pass

        _filament_id_name_cache = result
        _filament_id_name_cache_time = time.time()
        return result
    except Exception:
        return _filament_id_name_cache or {}
    finally:
        await cloud.close()


@router.get("/fields/{preset_type}")
async def get_preset_fields(
    preset_type: Literal["filament", "print", "process", "printer"],
    _: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Get field definitions for a preset type.

    Returns a list of field definitions including:
    - key: The setting key name
    - label: Human-readable label
    - type: Field type (text, number, boolean, select)
    - category: Grouping category
    - description: Field description
    - options: For select fields, available options
    - unit: Unit of measurement (if applicable)
    - min/max/step: For number fields, validation constraints
    """
    data = _load_fields(preset_type)
    return data


@router.get("/fields")
async def get_all_preset_fields(
    _: User | None = RequirePermission(Permission.CLOUD_AUTH),
):
    """
    Get all field definitions for all preset types.

    Returns field definitions organized by type.
    """
    return {
        "filament": _load_fields("filament"),
        "process": _load_fields("process"),
        "printer": _load_fields("printer"),
    }
