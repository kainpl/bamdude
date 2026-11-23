"""Spoolman integration service for syncing AMS filament data."""

import asyncio
import json
import logging
import math
import time
import weakref
from dataclasses import dataclass
from typing import Literal

import httpx

from backend.app.services.color_catalog_lookup import (
    CatalogColor,
    best_product_line_match,
    resolve_bambu_color,
    tray_product_line,
)
from backend.app.utils.color_utils import color_match_key, spoolman_color_hex

logger = logging.getLogger(__name__)

# "The caller asked no colour catalogue" — distinct from None, which is the
# catalogue's own answer "no row for this colour".
_COLOUR_NOT_RESOLVED = object()

BAMBU_RFID_TAG_LENGTH = 32

# How long a Spoolman without native tags is believed before it is asked
# again, so a Spoolman upgraded underneath needs no BamDude restart.
TAG_API_RECHECK_SECONDS = 600

# How long a native tag Spoolman refused for a spool is left alone: the AMS
# sync runs on every AMS update and would otherwise ask each time.
TAG_REFUSAL_RETRY_SECONDS = 600

# The AMS reports a Bambu chip's 4-byte UID padded to 8 bytes
# ("D3E68F32" arrives as "D3E68F3200000100"); a reader sees the 4 bytes.
AMS_CHIP_UID_PADDING = "00000100"

# What Spoolman strips from a tag uid before it stores or compares one
# (measured on 0.27.1); it upper-cases the rest.
_TAG_UID_SEPARATORS = str.maketrans("", "", ":-_ ")


def normalize_tag_uid(value: object) -> str:
    """A tag uid the way Spoolman stores it: quotes and separators off, upper case."""
    if not isinstance(value, str):
        return ""
    return value.strip().strip('"').translate(_TAG_UID_SEPARATORS).upper()


def is_linkable_tag_uid(uid: str) -> bool:
    """Whether a normalised uid may become a native tag.

    Spoolman takes only hex — and takes an all-zero uid, which is the AMS's
    "no tag read" and must never identify a spool.
    """
    return bool(uid) and all(ch in "0123456789ABCDEF" for ch in uid) and set(uid) != {"0"}


def native_tag_uids(spool: dict) -> list[str]:
    """The uids of a spool's native tags (Spoolman 0.27+), in Spoolman's order; none on an older server."""
    tags = spool.get("tags")
    if not isinstance(tags, list):
        return []
    return [uid for tag in tags if isinstance(tag, dict) and (uid := normalize_tag_uid(tag.get("uid")))]


def extra_tag(spool: dict) -> str:
    """The spool's ``extra.tag``, unquoted and upper-cased; anything that is not a string reads as no tag."""
    extra = spool.get("extra")
    raw = extra.get("tag") if isinstance(extra, dict) else None
    return raw.strip('"').upper() if isinstance(raw, str) else ""


def spool_tag_uids(spool: dict) -> set[str]:
    """Every identifier a spool is known by: its native tags and its ``extra.tag``."""
    uids = set(native_tag_uids(spool))
    if tag := extra_tag(spool):
        uids.add(tag)
    return uids


def ams_chip_uid(tag_uid: str, is_bambu: bool) -> str:
    """The chip's own uid out of what the AMS reports: a Bambu chip's padding comes off."""
    if is_bambu and tag_uid and len(tag_uid) == 16:
        return tag_uid[:8]
    return tag_uid


# Spool.extra key holding the consumed-counter baseline. Declared here so the
# client does not import from the routes package; ``_spoolman_helpers`` names
# the same key for the read side and a test pins the two equal.
BAMBU_WEIGHT_USED_BASELINE_KEY = "bambu_weight_used_baseline"


@dataclass
class SpoolmanSpool:
    """Represents a spool in Spoolman."""

    id: int
    filament_id: int | None
    remaining_weight: float | None
    used_weight: float
    first_used: str | None
    last_used: str | None
    location: str | None
    lot_nr: str | None
    comment: str | None
    extra: dict | None  # Contains tag_uid in extra.tag


@dataclass
class SpoolmanFilament:
    """Represents a filament type in Spoolman."""

    id: int
    name: str
    vendor_id: int | None
    material: str | None
    color_hex: str | None
    weight: float | None  # Net weight in grams


@dataclass
class AMSTray:
    """Represents an AMS tray with filament data from Bambu printer."""

    ams_id: int  # 0-3 for regular AMS, 128-135 for AMS-HT, 254+ for external spool
    tray_id: int  # 0-3
    tray_type: str  # PLA, PETG, ABS, etc.
    tray_sub_brands: str  # Full name like "PLA Basic", "PETG HF"
    tray_color: str  # Hex color like "FEC600FF"
    remain: int  # Remaining percentage (0-100)
    tag_uid: str  # RFID tag UID
    tray_uuid: str  # Spool UUID
    tray_info_idx: str  # Bambu filament preset ID like "GFA00"
    tray_weight: int  # Spool weight in grams (usually 1000)


class SpoolmanNotFoundError(Exception):
    """Raised when a spool ID does not exist in Spoolman (HTTP 404)."""


class SpoolmanUnavailableError(Exception):
    """Raised when Spoolman is unreachable or returns a server/network error."""


class SpoolmanClientError(Exception):
    """Raised when Spoolman returns a 4xx client error (not 404)."""

    def __init__(self, message: str, status_code: int, response_text: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class NativeTagConflict(Exception):
    """Spoolman refused a native tag: something else already holds that uid.

    ``spool_id`` names a spool holder (``archived`` says whether it is one the
    lists hide), ``filament_id`` a filament; both ``None`` is a holder
    Spoolman did not name.
    """

    def __init__(
        self, uid: str, *, spool_id: int | None = None, filament_id: int | None = None, archived: bool = False
    ) -> None:
        self.uid = uid
        self.spool_id = spool_id
        self.filament_id = filament_id
        self.archived = archived
        super().__init__(f"Tag {uid} belongs to {self.holder}")

    @property
    def holder(self) -> str:
        if self.spool_id is not None:
            return f"{'archived ' if self.archived else ''}spool {self.spool_id}"
        if self.filament_id is not None:
            return f"filament {self.filament_id}"
        return "something else in Spoolman"


def _filament_subtype_part(name: str, material: str) -> str:
    """Return the subtype portion of a filament name, lowercased.

    Mirrors the read-side derivation in
    ``backend/app/api/routes/_spoolman_helpers.py::_map_spoolman_spool``:
    if the filament name starts with the material prefix (e.g. ``"PLA Glow"``
    when material is ``"PLA"``), strip it; otherwise return the name as-is.

    Used by ``find_or_create_filament`` so an existing filament saved by the
    AMS-sync path with name ``"Glow"`` still matches a user-driven edit that
    composes ``"PLA Glow"`` (upstream Bambuddy #1357 / commit 4a98914d).
    """
    s = (name or "").strip()
    m = (material or "").strip()
    if m and s.upper().startswith(m.upper() + " "):
        return s[len(m) + 1 :].strip().lower()
    return s.lower()


class SpoolmanClient:
    """Client for interacting with Spoolman API."""

    def __init__(self, base_url: str):
        """Initialize the Spoolman client."""
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        # Per-spool locks for atomic read-modify-write in merge_spool_extra.
        # WeakValueDictionary: locks are GC'd once no coroutine holds a reference.
        self._extra_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        # Extra-field keys known to exist in this Spoolman (upstream 4e40a502 /
        # 4d2c6deb). Bounded by the keys BamDude writes, never by spool count;
        # per instance, so a client pointed at another Spoolman starts over.
        self._ensured_extra_fields: set[str] = set()
        self._ensure_extra_lock = asyncio.Lock()
        # Whether the extra-field LISTING has been read once — a separate
        # question from the set above ("have we asked" vs "what exists").
        self._extra_fields_listed = False
        # Whether this server links tags natively (Spoolman 0.27+); None until
        # asked. Per instance: a client for another URL asks for itself.
        self._tag_api: bool | None = None
        self._tag_api_asked_at = 0.0
        # (spool id, uid) -> when Spoolman last refused that tag for that spool.
        self._refused_tags: dict[tuple[int, str], float] = {}
        # tag -> when a spool the sync's list hides was found holding that reel.
        self._known_reels: dict[str, float] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client with connection pooling limits."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                follow_redirects=False,
                verify=True,
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        """Check if Spoolman server is reachable; returns True if healthy."""
        try:
            client = await self._get_client()
            response = await client.get(f"{self.api_url}/health")
            self._connected = response.status_code == 200
            return self._connected
        except Exception as e:
            logger.warning(
                "Spoolman health check failed (url=%s, type=%s): %s",
                self.api_url,
                type(e).__name__,
                e,
            )
            self._connected = False
            return False

    @property
    def is_connected(self) -> bool:
        """Check if client is connected to Spoolman."""
        return self._connected

    async def get_spools(self) -> list[dict]:
        """Fetch all spools from Spoolman with up to 3 retries on connection errors."""
        max_attempts = 3
        retry_delay = 0.5  # 500ms

        for attempt in range(1, max_attempts + 1):
            try:
                client = await self._get_client()
                response = await client.get(f"{self.api_url}/spool")
                response.raise_for_status()
                spools = response.json()
                if attempt > 1:
                    logger.info("Successfully fetched %d spools on attempt %d", len(spools), attempt)
                return spools
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as e:
                # Connection-related errors - close and recreate client for next attempt
                if attempt < max_attempts:
                    logger.warning(
                        "Connection error getting spools (attempt %d/%d): %s. Recreating client and retrying in %dms...",
                        attempt,
                        max_attempts,
                        e,
                        int(retry_delay * 1000),
                    )
                    # Close the stale client and recreate it
                    await self.close()
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error("Failed to get spools from Spoolman after %d attempts: %s", max_attempts, e)
                    raise SpoolmanUnavailableError("Cannot reach Spoolman") from e
            except Exception as e:
                # Other errors (HTTP errors, JSON decode errors, etc.)
                if attempt < max_attempts:
                    logger.warning(
                        "Failed to get spools from Spoolman (attempt %d/%d): %s. Retrying in %dms...",
                        attempt,
                        max_attempts,
                        e,
                        int(retry_delay * 1000),
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error("Failed to get spools from Spoolman after %d attempts: %s", max_attempts, e)
                    raise SpoolmanUnavailableError("Cannot reach Spoolman") from e

    async def _get_with_retry(self, path: str, params: dict | None = None) -> list[dict]:
        """GET a Spoolman JSON list endpoint with up to 3 retries on connection errors."""
        max_attempts = 3
        retry_delay = 0.5
        url = f"{self.api_url}/{path.lstrip('/')}"

        for attempt in range(1, max_attempts + 1):
            try:
                client = await self._get_client()
                response = await client.get(url, params=params or None)
                response.raise_for_status()
                return response.json()
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as e:
                if attempt < max_attempts:
                    logger.warning(
                        "Connection error fetching %s (attempt %d/%d): %s. Recreating client and retrying in %dms...",
                        path,
                        attempt,
                        max_attempts,
                        e,
                        int(retry_delay * 1000),
                    )
                    await self.close()
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error("Failed to fetch %s from Spoolman after %d attempts: %s", path, max_attempts, e)
                    raise SpoolmanUnavailableError("Cannot reach Spoolman") from e
            except Exception as e:
                if attempt < max_attempts:
                    logger.warning(
                        "Failed to fetch %s from Spoolman (attempt %d/%d): %s. Retrying in %dms...",
                        path,
                        attempt,
                        max_attempts,
                        e,
                        int(retry_delay * 1000),
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error("Failed to fetch %s from Spoolman after %d attempts: %s", path, max_attempts, e)
                    raise SpoolmanUnavailableError("Cannot reach Spoolman") from e

    async def get_filaments(self) -> list[dict]:
        """Fetch all internal filaments from Spoolman."""
        try:
            client = await self._get_client()
            response = await client.get(f"{self.api_url}/filament")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("Failed to get filaments from Spoolman: %s", e)
            raise SpoolmanUnavailableError("Cannot reach Spoolman") from e

    async def get_filament(self, filament_id: int) -> dict:
        """Fetch a single filament by ID from Spoolman."""
        if filament_id <= 0:
            raise ValueError(f"Invalid filament_id: {filament_id}")
        response = await self._request_filament("GET", filament_id, operation="get_filament")
        return response.json()

    async def get_external_filaments(self) -> list[dict]:
        """Fetch external/library filaments from Spoolman."""
        try:
            client = await self._get_client()
            response = await client.get(f"{self.api_url}/external/filament")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("Failed to get external filaments from Spoolman: %s", e)
            raise SpoolmanUnavailableError("Cannot reach Spoolman") from e

    async def get_vendors(self) -> list[dict]:
        """Fetch all vendors from Spoolman."""
        try:
            client = await self._get_client()
            response = await client.get(f"{self.api_url}/vendor")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("Failed to get vendors from Spoolman: %s", e)
            raise SpoolmanUnavailableError("Cannot reach Spoolman") from e

    async def create_vendor(self, name: str) -> dict:
        """Create a new vendor in Spoolman."""
        try:
            client = await self._get_client()
            response = await client.post(f"{self.api_url}/vendor", json={"name": name})
            if 400 <= response.status_code < 500:
                raise SpoolmanClientError(
                    f"Spoolman rejected vendor creation (HTTP {response.status_code})",
                    response.status_code,
                )
            response.raise_for_status()
            return response.json()
        except SpoolmanClientError:
            raise
        except Exception as e:
            logger.error("Failed to create vendor in Spoolman: %s", e)
            raise SpoolmanUnavailableError("Cannot reach Spoolman") from e

    def _get_material_density(self, material: str | None) -> float:
        """Return typical density (g/cm³) for the given filament material; defaults to PLA (1.24)."""
        # Typical densities for common filament materials
        densities = {
            "PLA": 1.24,
            "PLA-CF": 1.29,
            "PLA-S": 1.24,
            "PETG": 1.27,
            "ABS": 1.04,
            "ASA": 1.07,
            "TPU": 1.21,
            "PA": 1.14,  # Nylon
            "PA-CF": 1.20,
            "PC": 1.20,
            "PVA": 1.23,
            "HIPS": 1.04,
            "PP": 0.90,
            "PET": 1.38,
        }
        if material:
            # Try exact match first, then uppercase
            mat_upper = material.upper()
            for key, density in densities.items():
                if key.upper() == mat_upper or mat_upper.startswith(key.upper()):
                    return density
        return 1.24  # Default to PLA density

    async def create_filament(
        self,
        name: str,
        vendor_id: int | None = None,
        material: str | None = None,
        color_hex: str | None = None,
        color_name: str | None = None,
        weight: float | None = None,
        diameter: float = 1.75,
        density: float | None = None,
    ) -> dict:
        """Create a new filament in Spoolman."""
        if not name or not name.strip():
            raise ValueError("Filament name is required")

        if density is None:
            density = self._get_material_density(material)

        data: dict = {
            "name": name.strip(),
            "diameter": diameter,
            "density": density,
        }
        if vendor_id:
            data["vendor_id"] = vendor_id
        if material:
            data["material"] = material
        if color_hex:
            # Every create funnels through here, so this is where the stored
            # shape is decided: six characters for an opaque spool, eight only
            # when the alpha byte says the filament is translucent (#2912).
            data["color_hex"] = spoolman_color_hex(color_hex) or color_hex
        if color_name:
            data["color_name"] = color_name
        if weight:
            data["weight"] = weight

        logger.debug("Creating filament in Spoolman: %s", data)
        try:
            client = await self._get_client()
            response = await client.post(f"{self.api_url}/filament", json=data)
            if 400 <= response.status_code < 500:
                raise SpoolmanClientError(
                    f"Spoolman rejected filament creation (HTTP {response.status_code})",
                    response.status_code,
                )
            response.raise_for_status()
            return response.json()
        except SpoolmanClientError:
            raise
        except Exception as e:
            logger.error("Failed to create filament in Spoolman: %s", e)
            raise SpoolmanUnavailableError("Cannot reach Spoolman") from e

    async def patch_filament(self, filament_id: int, data: dict) -> dict:
        """PATCH a filament entry in Spoolman (e.g. update name or spool_weight)."""
        if filament_id <= 0:
            raise ValueError(f"Invalid filament_id: {filament_id}")
        response = await self._request_filament("PATCH", filament_id, json_body=data, operation="patch_filament")
        return response.json()

    async def create_spool(
        self,
        filament_id: int,
        remaining_weight: float | None = None,
        location: str | None = None,
        lot_nr: str | None = None,
        comment: str | None = None,
        extra: dict | None = None,
        initial_weight: float | None = None,
        spool_weight: float | None = None,
    ) -> dict:
        """Create a new spool in Spoolman."""
        data: dict = {"filament_id": filament_id}
        # The spool's own empty weight, only when the caller named one (0 is one).
        if spool_weight is not None:
            data["spool_weight"] = spool_weight
        # The spool's own net weight. Left off, Spoolman copies the filament's
        # weight, which is wrong for a spool of another size (upstream #3194).
        if initial_weight is not None:
            data["initial_weight"] = initial_weight
        if remaining_weight is not None:
            data["remaining_weight"] = remaining_weight
        if location:
            data["location"] = location
        if lot_nr:
            data["lot_nr"] = lot_nr
        if comment:
            data["comment"] = comment
        if extra:
            data["extra"] = extra
            await self._ensure_extra_fields(extra)

        logger.debug("Creating spool in Spoolman: %s", data)
        try:
            client = await self._get_client()
            response = await client.post(f"{self.api_url}/spool", json=data)
            if response.status_code == 404:
                raise SpoolmanNotFoundError(f"Filament {filament_id} not found in Spoolman")
            if 400 <= response.status_code < 500:
                raise SpoolmanClientError(
                    f"Spoolman rejected spool creation (HTTP {response.status_code})",
                    response.status_code,
                )
            response.raise_for_status()
            result = response.json()
            logger.info("Created spool %s in Spoolman", result.get("id"))
            return result
        except (SpoolmanNotFoundError, SpoolmanClientError):
            raise
        except Exception as e:
            logger.error("Failed to create spool in Spoolman: %s", e)
            raise SpoolmanUnavailableError("Cannot reach Spoolman") from e

    async def update_spool(
        self,
        spool_id: int,
        remaining_weight: float | None = None,
        location: str | None = None,
        clear_location: bool = False,
        extra: dict | None = None,
    ) -> dict:
        """Update an existing spool in Spoolman.

        ``last_used`` is NOT stamped (upstream f7bd06641): a sync, a location
        change and a weighing are not use. Print usage goes through ``/use``,
        which sets it itself.
        """
        data: dict = {}
        if remaining_weight is not None:
            data["remaining_weight"] = remaining_weight
        if clear_location:
            data["location"] = None
        elif location:
            data["location"] = location
        if extra:
            data["extra"] = extra
            await self._ensure_extra_fields(extra)

        response = await self._request_spool("PATCH", spool_id, json_body=data, operation="update")
        return response.json()

    async def _request_spool(
        self,
        method: Literal["GET", "PATCH", "DELETE"],
        spool_id: int,
        *,
        json_body: dict | None = None,
        operation: str,
    ) -> httpx.Response:
        """Perform a spool-scoped HTTP request, translating 404 and errors to named exceptions."""
        try:
            client = await self._get_client()
            response = await client.request(
                method,
                f"{self.api_url}/spool/{spool_id}",
                json=json_body,
            )
            if response.status_code == 404:
                raise SpoolmanNotFoundError(f"Spool {spool_id} not found in Spoolman")
            response.raise_for_status()
            return response
        except SpoolmanNotFoundError:
            raise
        except httpx.HTTPStatusError as e:
            if 400 <= e.response.status_code < 500:
                logger.warning(
                    "Spoolman returned %d for %s spool %s",
                    e.response.status_code,
                    operation,
                    spool_id,
                )
                raise SpoolmanClientError(
                    f"Spoolman rejected {operation} for spool {spool_id} (HTTP {e.response.status_code})",
                    e.response.status_code,
                    e.response.text[:500],
                ) from e
            else:
                logger.error("Failed to %s spool %s in Spoolman: %s", operation, spool_id, e)
                raise SpoolmanUnavailableError(f"Failed to {operation} spool {spool_id}") from e
        except Exception as e:
            logger.error("Failed to %s spool %s in Spoolman: %s", operation, spool_id, e)
            raise SpoolmanUnavailableError(f"Failed to {operation} spool {spool_id}") from e

    async def _request_filament(
        self,
        method: Literal["GET", "PATCH"],
        filament_id: int,
        *,
        json_body: dict | None = None,
        operation: str,
    ) -> httpx.Response:
        """Perform a filament-scoped HTTP request, translating 404 and errors to named exceptions."""
        try:
            client = await self._get_client()
            response = await client.request(
                method,
                f"{self.api_url}/filament/{filament_id}",
                json=json_body,
            )
            if response.status_code == 404:
                raise SpoolmanNotFoundError(f"Filament {filament_id} not found in Spoolman")
            response.raise_for_status()
            return response
        except SpoolmanNotFoundError:
            raise
        except httpx.HTTPStatusError as e:
            if 400 <= e.response.status_code < 500:
                logger.warning(
                    "Spoolman returned %d for %s filament %s",
                    e.response.status_code,
                    operation,
                    filament_id,
                )
                raise SpoolmanClientError(
                    f"Spoolman rejected {operation} for filament {filament_id} (HTTP {e.response.status_code})",
                    e.response.status_code,
                    e.response.text[:500],
                ) from e
            else:
                logger.error("Failed to %s filament %s in Spoolman: %s", operation, filament_id, e)
                raise SpoolmanUnavailableError(f"Failed to {operation} filament {filament_id}") from e
        except Exception as e:
            logger.error("Failed to %s filament %s in Spoolman: %s", operation, filament_id, e)
            raise SpoolmanUnavailableError(f"Failed to {operation} filament {filament_id}") from e

    async def get_spool(self, spool_id: int) -> dict:
        """Fetch a single spool by ID from Spoolman."""
        response = await self._request_spool("GET", spool_id, operation="get")
        return response.json()

    async def get_all_spools(self, allow_archived: bool = False) -> list[dict]:
        """Fetch all spools from Spoolman with retry, optionally including archived ones."""
        params: dict = {}
        if allow_archived:
            params["allow_archived"] = "true"
        return await self._get_with_retry("/spool", params=params or None)

    async def get_distinct_locations(self) -> list[str]:
        """Return distinct location strings currently assigned to Spoolman spools.

        Spoolman's `/location` endpoint shape varies across versions: older
        releases return `list[str]`, newer ones return `list[dict]` with a
        `name` field. Normalize to `list[str]` so callers can iterate without
        runtime shape checks.
        """
        raw = await self._get_with_retry("/location")
        if not isinstance(raw, list):
            return []
        names: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str):
                    names.append(name)
        return names

    async def rename_location(self, current_name: str, new_name: str) -> int:
        """Bulk-rename a location string on all Spoolman spools.

        Tries the bulk `PATCH /location/{name}` endpoint first. Spoolman
        versions older than ~0.16 don't expose it and respond 404/405 — in
        that case fall back to iterating every spool currently at
        ``current_name`` and PATCHing each one's ``location`` field directly.
        Returns the number of spools renamed (or 0 if the bulk endpoint
        succeeded without enumerating).
        """
        from urllib.parse import quote

        encoded = quote(current_name, safe="")
        client = await self._get_client()
        try:
            response = await client.patch(
                f"{self.api_url}/location/{encoded}",
                json={"name": new_name},
            )
            response.raise_for_status()
            return 0
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404, 405):
                raise
            logger.info(
                "Spoolman bulk-rename endpoint unavailable (status %d); falling back to per-spool PATCH",
                exc.response.status_code,
            )

        # Per-spool fallback: enumerate every spool currently at the old name
        # and PATCH each. Keep going on individual failures so a single
        # already-deleted spool doesn't strand the rest at the old name —
        # collect errors and re-raise as a single SpoolmanClientError if any
        # leftover survives.
        spools = await self.get_all_spools(allow_archived=True)
        renamed = 0
        failures: list[str] = []
        for spool in spools:
            if (spool.get("location") or "").strip() != current_name:
                continue
            try:
                await self._request_spool(
                    "PATCH",
                    spool["id"],
                    json_body={"location": new_name},
                    operation="rename-location",
                )
                renamed += 1
            except SpoolmanNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001 — accumulate and re-raise below
                failures.append(f"spool {spool.get('id')}: {exc}")
        if failures:
            raise SpoolmanClientError(
                f"Spoolman rename fallback failed for {len(failures)} spool(s): {'; '.join(failures[:3])}",
                status_code=502,
            )
        return renamed

    async def delete_spool(self, spool_id: int) -> None:
        """Delete a spool from Spoolman."""
        await self._request_spool("DELETE", spool_id, operation="delete")

    async def is_filament_shared(self, filament_id: int, exclude_spool_id: int) -> bool:
        """True iff any spool other than ``exclude_spool_id`` is linked to
        ``filament_id``.

        Used by the spool-edit path to decide between PATCHing the existing
        filament in place (singleton) and falling back to
        ``find_or_create_filament`` (shared — re-linking the spool is the
        only safe option). Includes archived spools so a shared link
        doesn't suddenly look singleton just because the sibling spool was
        archived. Upstream Bambuddy #1357 follow-up / commit 3b552094.
        """
        spools = await self.get_all_spools(allow_archived=True)
        for s in spools:
            if s.get("id") == exclude_spool_id:
                continue
            if ((s.get("filament") or {}).get("id")) == filament_id:
                return True
        return False

    async def set_spool_archived(self, spool_id: int, archived: bool) -> dict:
        """Archive or restore a spool in Spoolman."""
        response = await self._request_spool(
            "PATCH",
            spool_id,
            json_body={"archived": archived},
            operation="archive/restore",
        )
        return response.json()

    async def reset_spool_consumed_counter(self, spool_id: int) -> dict:
        """Zero the shown consumed counter by recording a baseline in ``spool.extra``.

        This used to PATCH ``used_weight = 0``, which is not a baseline:
        Spoolman derives ``remaining_weight`` from initial minus used, so the
        spool went back to full and its measured remainder was lost (upstream
        #2906). Writing the baseline into ``extra`` touches no native Spoolman
        field; ``_map_spoolman_spool`` folds it back into the baseline it
        reports — the mechanism internal mode has in its
        ``weight_used_baseline`` column.

        Stored as a JSON *string* of the number: Spoolman registers an unseen
        extra key as a ``text`` field, which refuses a value that does not
        decode to a string, and a field's type cannot be changed afterwards.

        Serialised on the per-spool lock ``merge_spool_extra`` uses, so a
        concurrent tag or colour write cannot read the extra dict between
        this fetch and its PATCH and put the old one back.
        """
        async with self.extra_lock(spool_id):
            current = await self.get_spool(spool_id)  # raises on error
            used_weight = current.get("used_weight")
            try:
                baseline = float(used_weight) if used_weight is not None else 0.0
            except (TypeError, ValueError):
                baseline = 0.0
            if not math.isfinite(baseline) or baseline < 0:
                baseline = 0.0
            merged = {
                **(current.get("extra") or {}),
                BAMBU_WEIGHT_USED_BASELINE_KEY: json.dumps(str(baseline)),
            }
            return await self.update_spool_full(spool_id=spool_id, extra=merged)

    async def update_spool_full(
        self,
        spool_id: int,
        *,
        filament_id: int | None = None,
        remaining_weight: float | None = None,
        comment: str | None = None,
        price: float | None = None,
        location: str | None = None,
        clear_location: bool = False,
        extra: dict | None = None,
        spool_weight: float | None = None,
        clear_spool_weight: bool = False,
        initial_weight: float | None = None,
    ) -> dict:
        """Update a spool with full field support; unlike update_spool, does not auto-set last_used."""
        data: dict = {}
        if filament_id is not None:
            data["filament_id"] = filament_id
        # Spoolman applies ``initial_weight`` before ``remaining_weight``, so a
        # remaining weight in the same PATCH is measured against the new size.
        if initial_weight is not None:
            data["initial_weight"] = initial_weight
        if remaining_weight is not None:
            data["remaining_weight"] = remaining_weight
        if comment is not None:
            data["comment"] = comment if comment else None
        if price is not None:
            data["price"] = price
        if clear_location:
            data["location"] = None
        elif location is not None:
            data["location"] = location
        if extra is not None:
            data["extra"] = extra
            await self._ensure_extra_fields(extra)
        if clear_spool_weight:
            data["spool_weight"] = None
        elif spool_weight is not None:
            data["spool_weight"] = spool_weight

        response = await self._request_spool("PATCH", spool_id, json_body=data, operation="update")
        return response.json()

    def extra_lock(self, spool_id: int) -> asyncio.Lock:
        """Return (creating if needed) the per-spool asyncio.Lock used by merge_spool_extra."""
        lock = self._extra_locks.get(spool_id)
        if lock is None:
            lock = asyncio.Lock()
            self._extra_locks[spool_id] = lock
        return lock

    async def merge_spool_extra(self, spool_id: int, new_fields: dict) -> dict:
        """Fetch the spool's extra dict, merge new_fields into it, then PATCH back — serialised per spool."""
        async with self.extra_lock(spool_id):
            current = await self.get_spool(spool_id)  # raises on error
            current_extra: dict = current.get("extra") or {}
            merged = {**current_extra, **new_fields}
            return await self.update_spool_full(spool_id=spool_id, extra=merged)

    async def find_or_create_vendor(self, name: str) -> int:
        """Return the Spoolman vendor ID for the given name, creating the vendor if absent."""
        vendors = await self.get_vendors()
        name_lower = name.strip().lower()
        for vendor in vendors:
            if vendor.get("name", "").strip().lower() == name_lower:
                return vendor["id"]
        created = await self.create_vendor(name.strip())
        vendor_id = created.get("id")
        if not vendor_id:
            raise SpoolmanUnavailableError(f"Spoolman returned vendor without id field: {list(created.keys())}")
        return vendor_id

    async def find_or_create_filament(
        self,
        material: str,
        subtype: str,
        brand: str | None,
        color_hex: str,
        label_weight: int,
        color_name: str | None = None,
        *,
        filament_name: str | None = None,
    ) -> int:
        """Return the filament ID matching material/name/brand/color, creating it if absent."""
        name = (
            filament_name if filament_name is not None else (f"{material} {subtype}".strip() if subtype else material)
        )
        # One value in both roles: the key the loop below compares on and the
        # value a new filament is created with are the same string, so an opaque
        # spool keys and stores as six characters, a translucent one as eight,
        # and neither is conflated with the other (#2912).
        color = color_match_key(color_hex)

        vendor_id: int | None = None
        if brand:
            vendor_id = await self.find_or_create_vendor(brand)

        # Normalised match keys (case-insensitive). Computed once outside
        # the loop so the inner comparison stays simple.
        composed_subtype = _filament_subtype_part(name, material)
        material_norm = material.upper()
        brand_norm = (brand or "").strip().lower()

        filaments = await self.get_filaments()
        for f in filaments:
            f_material = (f.get("material") or "").upper()
            f_color = color_match_key(f.get("color_hex"))
            f_vendor = f.get("vendor") or {}
            f_vendor_name = (f_vendor.get("name") or "").strip().lower()

            material_match = f_material == material_norm
            # Match on the SUBTYPE PORTION of the filament name. AMS-sync
            # auto-create (the underscore-prefixed ``_find_or_create_filament``
            # used during MQTT tray import) stores the filament as just
            # ``tray.tray_sub_brands`` — e.g. ``"Glow"`` — while the
            # user-driven edit path here composes ``"<material> <subtype>"``
            # — ``"PLA Glow"``. The old literal-equality ``f_name == name``
            # check failed to bridge the two shapes, so every edit fell
            # through to ``create_filament``, leaving a trail of duplicate
            # filaments AND leaving the spool either still pointed at the
            # old filament whose ``color_name`` never got patched, or at a
            # new filament with the colour while the inventory list kept
            # showing the synth fallback from the old one. Upstream
            # Bambuddy #1357 / commit 4a98914d.
            f_name = f.get("name") or ""
            name_match = (
                f_name == filament_name
                if filament_name is not None
                else _filament_subtype_part(f_name, material) == composed_subtype
            )
            color_match = f_color == color
            vendor_match = (not brand) or f_vendor_name == brand_norm

            if material_match and name_match and color_match and vendor_match:
                # color_name is intentionally NOT part of the match key and
                # is no longer patched onto the filament here: Spoolman 0.23.1
                # has no ``color_name`` field on Filament (#1357 — confirmed
                # against the FilamentUpdateParameters schema). The earlier
                # attempt to PATCH it silently dropped the key, which was
                # exactly why the user's edit looked "not saved". The route
                # now persists color_name via ``spool.extra.bambu_color_name``
                # (see ``_map_spoolman_spool`` for the read side);
                # ``find_or_create_filament``'s only job is to resolve the
                # right filament_id for the spool link.
                return f["id"]

        # color_name omitted: Spoolman has no such field on Filament (#1357);
        # the user's color_name lands in ``spool.extra.bambu_color_name`` via
        # the route after ``find_or_create_filament`` returns the new id.
        filament = await self.create_filament(
            name=name,
            vendor_id=vendor_id,
            material=material,
            color_hex=color,
            weight=float(label_weight),
        )
        filament_id = filament.get("id")
        if not filament_id:
            raise SpoolmanUnavailableError(f"Spoolman returned filament without id field: {list(filament.keys())}")
        return filament_id

    async def use_spool(self, spool_id: int, used_weight: float) -> dict:
        """Record filament usage for a spool via the Spoolman /use endpoint."""
        try:
            client = await self._get_client()
            response = await client.put(
                f"{self.api_url}/spool/{spool_id}/use",
                json={"use_weight": used_weight},
            )
            if response.status_code == 404:
                raise SpoolmanNotFoundError(f"Spool {spool_id} not found in Spoolman")
            if 400 <= response.status_code < 500:
                raise SpoolmanClientError(
                    f"Spoolman rejected use_spool for spool {spool_id} (HTTP {response.status_code})",
                    response.status_code,
                )
            response.raise_for_status()
            return response.json()
        except (SpoolmanNotFoundError, SpoolmanClientError):
            raise
        except Exception as e:
            logger.error("Failed to record spool usage in Spoolman: %s", e)
            raise SpoolmanUnavailableError(f"Failed to record usage for spool {spool_id}") from e

    async def has_tag_api(self) -> bool:
        """Whether this Spoolman links tags natively (0.27+).

        The tag routes themselves are asked, not a version number. An answer —
        yes or no — is asked again after ``TAG_API_RECHECK_SECONDS``, so a
        Spoolman upgraded or downgraded underneath needs no BamDude restart; a
        Spoolman that did not answer, or answered with a server error, is not
        remembered, and the last answer it gave stands meanwhile.
        """
        stale = self._tag_api is not None and time.monotonic() - self._tag_api_asked_at > TAG_API_RECHECK_SECONDS
        if self._tag_api is None or stale:
            try:
                client = await self._get_client()
                response = await client.get(f"{self.api_url}/tag/reader")
            except httpx.HTTPError as e:
                logger.debug("Spoolman native-tag probe failed: %s", e)
                return bool(self._tag_api)
            if response.status_code >= 500:
                return bool(self._tag_api)
            capable = response.status_code == 200
            if capable:
                try:
                    capable = isinstance(response.json(), list)
                except ValueError:
                    capable = False
            if capable != self._tag_api:
                logger.info("Spoolman native tags: %s", "available" if capable else "not available")
            self._tag_api = capable
            self._tag_api_asked_at = time.monotonic()
        return bool(self._tag_api)

    async def _tag_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """A request to Spoolman's tag routes, failures as the typed exceptions.

        400 and 409 come back as responses: they are answers the callers act on.
        """
        try:
            client = await self._get_client()
            response = await client.request(method, f"{self.api_url}{path}", **kwargs)
        except httpx.HTTPError as e:
            raise SpoolmanUnavailableError(f"Cannot reach Spoolman for {method} {path}") from e
        if response.status_code in (400, 409) or response.status_code < 400:
            return response
        if response.status_code == 404:
            raise SpoolmanNotFoundError(f"Spoolman answered 404 for {method} {path}")
        if response.status_code < 500:
            raise SpoolmanClientError(
                f"Spoolman rejected {method} {path} (HTTP {response.status_code})",
                response.status_code,
                response.text[:500],
            )
        raise SpoolmanUnavailableError(f"Spoolman failed {method} {path} (HTTP {response.status_code})")

    async def find_spool_by_native_tag(self, uid: str, *, allow_archived: bool = False) -> dict | None:
        """The spool a native tag is linked to, asked of the server, or None.

        Only a spool that really carries the uid counts: a server that does
        not know ``?tag=`` ignores it and answers every spool.
        """
        wanted = normalize_tag_uid(uid)
        if not wanted:
            return None
        params = {"tag": wanted}
        if allow_archived:
            params["allow_archived"] = "true"
        response = await self._tag_request("GET", "/spool", params=params)
        if response.status_code == 400:
            return None  # not a hex uid: no spool can hold it
        try:
            spools = response.json()
        except ValueError:
            return None
        if not isinstance(spools, list):
            return None
        return next((s for s in spools if isinstance(s, dict) and wanted in native_tag_uids(s)), None)

    async def link_native_tag(self, spool_id: int, uid: str, tag_format: str | None = None) -> None:
        """Link one native tag to a spool; ``NativeTagConflict`` when something else holds the uid."""
        body: dict = {"uid": uid}
        if tag_format:
            body["format"] = tag_format
        response = await self._tag_request("POST", f"/spool/{spool_id}/tag", json=body)
        if response.status_code == 400:
            raise SpoolmanClientError(f"Spoolman refused tag {uid!r}", 400, response.text[:500])
        if response.status_code != 409:
            return
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        holder = answer.get("spool_id") if isinstance(answer, dict) else None
        filament = answer.get("filament_id") if isinstance(answer, dict) else None
        if not isinstance(holder, int) or isinstance(holder, bool):
            raise NativeTagConflict(uid, filament_id=filament if isinstance(filament, int) else None)
        archived = False
        try:
            archived = bool((await self.get_spool(holder)).get("archived"))
        except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError):
            pass  # the holder is still named; only "archived" is left unsaid
        raise NativeTagConflict(uid, spool_id=holder, archived=archived)

    async def unlink_native_tag(self, spool_id: int, uid: str) -> None:
        """Remove a native tag from a spool; a tag the spool does not hold is not an error."""
        try:
            await self._tag_request("DELETE", f"/spool/{spool_id}/tag/{uid}")
        except SpoolmanNotFoundError:
            pass

    async def unlink_all_native_tags(self, spool: dict) -> None:
        """Remove every native tag a spool carries. A spool without any costs no request."""
        for uid in native_tag_uids(spool):
            await self.unlink_native_tag(int(spool["id"]), uid)

    async def take_back_native_tags(self, spool_id: int, uids: list[str]) -> None:
        """Undo native tags a half-done link added. Best-effort: what cannot be removed is logged."""
        for uid in uids:
            try:
                await self.unlink_native_tag(spool_id, uid)
            except (SpoolmanClientError, SpoolmanUnavailableError) as exc:
                logger.warning("Could not take back native tag %s from spool %s: %s", uid, spool_id, exc)

    async def link_native_tags(self, spool_id: int, uids: list[str | None], tag_format: str | None = None) -> list[str]:
        """Link the given uids to a spool as native tags — the MANUAL path: all of them or none.

        Returns the uids this call added (to take back if a later step of the
        caller fails); nothing on a Spoolman without native tags. A uid held
        elsewhere — by an active spool, an ARCHIVED one or a filament — raises
        ``NativeTagConflict`` after what this call had added is taken back:
        the holder keeps its tag and the operator is told who it is.
        """
        wanted = list(dict.fromkeys(u for raw in uids if is_linkable_tag_uid(u := normalize_tag_uid(raw))))
        if not wanted or not await self.has_tag_api():
            return []
        had = set(native_tag_uids(await self.get_spool(spool_id)))
        added: list[str] = []
        try:
            for uid in wanted:
                if uid in had:
                    continue
                await self.link_native_tag(spool_id, uid, tag_format)
                added.append(uid)
        except (NativeTagConflict, SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError):
            await self.take_back_native_tags(spool_id, added)
            raise
        return added

    async def drop_previous_reel_tags(self, spool: dict, kept: list[str | None], *, bambu_reel: bool) -> list[str]:
        """After a MANUAL re-link: remove the native tags of the reel the spool was linked to before.

        ``spool`` is the spool as it was before ``extra.tag`` was replaced;
        ``kept`` the uids of the new link. With ``extra.tag`` alone a re-link
        replaced the value; native tags pile up, and a reader asks them first,
        so the reel linked by mistake went on resolving to this spool.

        Removed: the previous ``extra.tag`` (and the chip's own form of an
        AMS-padded one), and — when the new link is a Bambu reel — every other
        tag of a Bambu reel, because one spool is one reel. A tag of any other
        kind is somebody else's (Spoolman's UI, a reader) and stays. Nothing is
        removed when the new link names no physical tag (a slot's generated
        id). Best-effort: what cannot be removed is logged. Returns what went.
        """
        keep = {uid for raw in kept if (uid := normalize_tag_uid(raw))}
        tags = spool.get("tags")
        if not keep or not isinstance(tags, list):
            return []
        previous = extra_tag(spool)
        stale = {previous}
        if len(previous) == 16 and previous.endswith(AMS_CHIP_UID_PADDING):
            stale.add(previous[:8])
        removed: list[str] = []
        for tag in tags:
            uid = normalize_tag_uid(tag.get("uid")) if isinstance(tag, dict) else ""
            if not uid or uid in keep:
                continue
            if uid not in stale and not (bambu_reel and tag.get("format") == "bambu"):
                continue
            try:
                await self.unlink_native_tag(int(spool["id"]), uid)
            except (SpoolmanClientError, SpoolmanUnavailableError) as exc:
                logger.warning("Could not remove the previous tag %s from spool %s: %s", uid, spool.get("id"), exc)
                continue
            removed.append(uid)
        if removed:
            logger.info("Re-linked Spoolman spool %s: removed its previous tags %s", spool.get("id"), removed)
        return removed

    async def add_native_tags(self, spool: dict, uids: list[str | None], tag_format: str | None = None) -> int:
        """Link the given uids to a spool as native tags — the AUTOMATIC path: best-effort.

        A uid something else holds, or a failed request, is logged with the
        holder and skipped, so the sync never loses its result over this — and
        never makes another spool for the reel: ``extra.tag`` is written
        beside the native tags and still finds this one. A refused uid is not
        asked for again for ``TAG_REFUSAL_RETRY_SECONDS`` and warned about
        once. Returns how many tags were added.
        """
        wanted = list(dict.fromkeys(u for raw in uids if is_linkable_tag_uid(u := normalize_tag_uid(raw))))
        have = set(native_tag_uids(spool))
        missing = [uid for uid in wanted if uid not in have]
        if not missing or not await self.has_tag_api():
            return 0
        spool_id = int(spool["id"])
        now = time.monotonic()
        added = 0
        for uid in missing:
            refused_at = self._refused_tags.get((spool_id, uid))
            if refused_at is not None and now - refused_at < TAG_REFUSAL_RETRY_SECONDS:
                continue
            try:
                await self.link_native_tag(spool_id, uid, tag_format)
            except NativeTagConflict as conflict:
                log = logger.debug if refused_at is not None else logger.warning
                log("Native tag %s belongs to %s, not added to spool %s", uid, conflict.holder, spool_id)
                self._refused_tags[(spool_id, uid)] = now
                continue
            except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as e:
                logger.warning("Could not add native tag %s to spool %s: %s", uid, spool_id, e)
                continue
            added += 1
            self._refused_tags.pop((spool_id, uid), None)
            # Keep the caller's copy (the sync's cached list) true to Spoolman.
            if isinstance(spool.get("tags"), list):
                spool["tags"].append({"uid": uid, **({"format": tag_format} if tag_format else {})})
            else:
                spool["tags"] = [{"uid": uid, **({"format": tag_format} if tag_format else {})}]
            logger.info("Added native tag %s to Spoolman spool %s", uid, spool_id)
        return added

    async def _holder_outside_the_list(self, uids: list[str | None]) -> dict | None:
        """A spool the sync's list did not show that holds one of these uids natively.

        Asked before a spool is created for a reel nobody matched: the listing
        hides archived spools, and an archived spool's reel is a known reel.
        """
        if not await self.has_tag_api():
            return None
        for uid in dict.fromkeys(u for raw in uids if is_linkable_tag_uid(u := normalize_tag_uid(raw))):
            try:
                holder = await self.find_spool_by_native_tag(uid, allow_archived=True)
            except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as e:
                logger.debug("Could not ask Spoolman who holds tag %s: %s", uid, e)
                continue
            if holder is not None:
                return holder
        return None

    async def reel_holder(self, tray: AMSTray) -> dict | None:
        """The spool — an archived one included — that natively holds this tray's
        reel by any of its identifiers, or None. For a route that must say WHY
        no spool was made for a slot."""
        return await self._holder_outside_the_list(
            [tray.tray_uuid, tray.tag_uid, ams_chip_uid(tray.tag_uid or "", True)]
        )

    async def find_spool_by_tag(self, tag_uid: str, cached_spools: list[dict] | None = None) -> dict | None:
        """Return the spool matching the given RFID tag UID, or None if not found.

        Native tags (Spoolman 0.27+) are asked first — out of the cached list
        when there is one, otherwise with one server-side query — and
        ``extra.tag`` after them, for spools linked before and for older
        servers.
        """
        native_tag = normalize_tag_uid(tag_uid)
        if native_tag and cached_spools is not None:
            for spool in cached_spools:
                if native_tag in native_tag_uids(spool):
                    return spool
        elif native_tag and await self.has_tag_api():
            try:
                spool = await self.find_spool_by_native_tag(native_tag)
            except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as e:
                logger.warning("Native tag lookup failed, falling back to extra.tag: %s", e)
                spool = None
            if spool is not None:
                return spool

        # Use cached spools if provided, otherwise fetch from API
        spools = cached_spools if cached_spools is not None else await self.get_spools()
        # Normalize tag_uid for comparison (uppercase, strip quotes)
        search_tag = tag_uid.strip('"').upper()

        for spool in spools:
            extra = spool.get("extra", {})
            if extra:
                stored_tag = extra.get("tag", "")
                # Normalize stored tag (strip quotes, uppercase)
                if stored_tag:
                    normalized_tag = stored_tag.strip('"').upper()
                    if normalized_tag == search_tag:
                        logger.debug("Found spool %s matching tag %s", spool["id"], tag_uid)
                        return spool
        return None

    async def find_spools_by_location_prefix(
        self, location_prefix: str, cached_spools: list[dict] | None = None
    ) -> list[dict]:
        """Return all spools whose location starts with location_prefix."""
        # Use cached spools if provided, otherwise fetch from API
        spools = cached_spools if cached_spools is not None else await self.get_spools()
        matching = []
        for spool in spools:
            location = spool.get("location", "")
            if location and location.startswith(location_prefix):
                matching.append(spool)
        return matching

    async def clear_location_for_removed_spools(
        self,
        printer_name: str,
        current_tray_uuids: set[str],
        cached_spools: list[dict] | None = None,
        synced_spool_ids: set[int] | None = None,
    ) -> int:
        """Clear location for Bambu Lab spools at this printer whose tray_uuid is no longer in the AMS."""
        location_prefix = f"{printer_name} - "
        spools_at_printer = await self.find_spools_by_location_prefix(location_prefix, cached_spools=cached_spools)
        cleared_count = 0

        for spool in spools_at_printer:
            spool_id = spool.get("id")

            # Skip spools that were just synced (matched by location or tag)
            if synced_spool_ids and spool_id in synced_spool_ids:
                continue

            # Get the tray_uuid (stored as "tag" in extra field)
            extra = spool.get("extra", {}) or {}
            stored_tag = extra.get("tag", "")
            if stored_tag:
                # Normalize: strip quotes and uppercase
                spool_uuid = stored_tag.strip('"').upper()
            else:
                spool_uuid = ""

            # Only clear location for Bambu Lab spools (those with a stored 32-character RFID tag).
            if len(spool_uuid) != BAMBU_RFID_TAG_LENGTH:
                continue

            # If this spool's UUID is not in the current AMS, clear its location
            if spool_uuid not in current_tray_uuids:
                logger.info(
                    f"Clearing location for spool {spool_id} "
                    f"(was: {spool.get('location')}, uuid: {spool_uuid[:16] if spool_uuid else 'none'}...)"
                )
                result = await self.update_spool(spool_id=spool_id, clear_location=True)
                if result:
                    cleared_count += 1

        return cleared_count

    async def ensure_bambu_vendor(self) -> int | None:
        """Return the Bambu Lab vendor ID in Spoolman, creating the vendor if absent."""
        vendors = await self.get_vendors()
        for vendor in vendors:
            if vendor.get("name", "").lower() == "bambu lab":
                return vendor["id"]

        # Create Bambu Lab vendor if not exists
        vendor = await self.create_vendor("Bambu Lab")
        return vendor["id"] if vendor else None

    async def ensure_tag_extra_field(self) -> bool:
        """Register the 'tag' extra field in Spoolman if not present; returns True on success."""
        return await self.ensure_extra_field("tag")

    async def _load_existing_extra_field_keys(self) -> set[str] | None:
        """Keys of the spool extra fields Spoolman already has, or ``None`` when
        the listing could not be read (upstream 4d2c6deb).

        ``None`` and ``set()`` differ and the caller acts on it: empty is
        "Spoolman has none", so every needed field is created; ``None`` is "we
        could not find out", where the only safe move is to attempt the write
        blind. Matched on ``key``, never the display ``name`` — a field the user
        renamed in Spoolman's UI is the same field, and reading it as missing is
        what re-created it.
        """
        try:
            client = await self._get_client()
            response = await client.get(f"{self.api_url}/field/spool")
            if response.status_code != 200:
                logger.debug(
                    "Spoolman extra-field listing returned %s; falling back to blind registration",
                    response.status_code,
                )
                return None
            fields = response.json()
        except Exception as e:  # noqa: BLE001 — registration is best-effort, see _ensure_extra_fields
            logger.debug("Could not read Spoolman extra-field listing: %s", e)
            return None
        if not isinstance(fields, list):
            return None
        return {f["key"] for f in fields if isinstance(f, dict) and isinstance(f.get("key"), str)}

    async def ensure_extra_field(self, name: str, field_type: str = "text") -> bool:
        """Register a custom extra field in Spoolman if not present.

        Spoolman rejects a write that carries an unknown extra-dict key with
        HTTP 400 ('Unknown extra field <name>.'), so any custom field BamDude
        persists alongside spools needs to be registered first. Idempotent —
        returns True if the field already exists.

        Existence is read from ``GET /field/spool``, the whole listing, once per
        client (upstream 4d2c6deb). The old per-field ``GET /field/spool/{name}``
        was never served — Spoolman declares only POST and DELETE there, so it
        answered 405 every time — and falling through to the POST was worse than
        a wasted request: that endpoint is an UPSERT, so a field the user had
        renamed, retyped or given a default in Spoolman was reset to BamDude's
        version on every restart. Only a successful listing is banked, so one
        transient failure does not leave the client posting blind for its life.
        """
        try:
            if name in self._ensured_extra_fields:
                return True
            if not self._extra_fields_listed:
                existing = await self._load_existing_extra_field_keys()
                if existing is not None:
                    self._ensured_extra_fields |= existing
                    self._extra_fields_listed = True
                    if name in existing:
                        logger.debug("Spoolman extra field %r already exists", name)
                        return True

            client = await self._get_client()
            field_data = {
                "name": name,
                "field_type": field_type,
                "default_value": None,
            }
            response = await client.post(f"{self.api_url}/field/spool/{name}", json=field_data)
            if response.status_code in (200, 201):
                logger.info("Created Spoolman extra field %r", name)
                self._ensured_extra_fields.add(name)
                return True

            logger.warning(
                "Failed to create Spoolman extra field %r: %s - %s",
                name,
                response.status_code,
                response.text,
            )
            return False

        except Exception as e:
            logger.warning("Failed to ensure Spoolman extra field %r exists: %s", name, e)
            return False

    async def _ensure_extra_fields(self, extra: dict | None) -> None:
        """Register every extra key an outgoing write carries, once per client
        (upstream 4e40a502).

        Registration used to happen before the FEATURE, from hand-kept lists (the
        connect route, two inline blocks in the inventory routes) — and enabling
        Spoolman from Settings reaches none of them, so the first AMS sync on a
        fresh Spoolman failed on every slot with "Unknown extra field tag.". Keyed
        off the payload, a write that carries a key is a write that registers it.

        Best-effort: ``ensure_extra_field`` logs and returns False rather than
        raising, and a failure must not turn a write that might still succeed
        into one that never happens. Failures are not remembered, so a Spoolman
        that was restarting gets another try on the next write.
        """
        names = [name for name in (extra or {}) if name not in self._ensured_extra_fields]
        if not names:
            return
        async with self._ensure_extra_lock:
            for name in names:
                # Re-checked under the lock: a concurrent write may have just
                # registered it, and two syncs racing to POST the same field is
                # how one of them logs a needless warning.
                if name not in self._ensured_extra_fields:
                    await self.ensure_extra_field(name)

    def parse_ams_tray(self, ams_id: int, tray_data: dict) -> AMSTray | None:
        """Parse raw MQTT tray data into an AMSTray; returns None for empty or invalid trays."""
        # Skip empty trays - check for valid tray_type
        tray_type = tray_data.get("tray_type", "")
        if not tray_type or tray_type.strip() == "":
            return None

        # Need valid color to create filament
        tray_color = tray_data.get("tray_color", "")
        if not tray_color or tray_color.strip() == "":
            logger.debug("Skipping tray with empty color")
            return None

        # Transparent filament (alpha=00) used to be rewritten to a cream
        # "natural PLA" colour here because the swatch renderer couldn't show
        # alpha. The swatch now paints a checkerboard underlay for translucent
        # rgbas, so we pass ``00000000`` through verbatim — the inventory row
        # keeps the AMS-reported colour and the frontend resolves the name to
        # "Clear" via getColorName (#1545).

        # Get sub_brands, falling back to tray_type
        tray_sub_brands = tray_data.get("tray_sub_brands", "")
        if not tray_sub_brands or tray_sub_brands.strip() == "":
            tray_sub_brands = tray_type

        # Get tag_uid and tray_uuid, filtering out empty/invalid values
        tag_uid = tray_data.get("tag_uid", "")
        if tag_uid in ("", "0000000000000000"):
            tag_uid = ""
        tray_uuid = tray_data.get("tray_uuid", "")
        if tray_uuid in ("", "00000000000000000000000000000000"):
            tray_uuid = ""

        # Get tray_info_idx (Bambu filament preset ID like "GFA00")
        tray_info_idx = tray_data.get("tray_info_idx", "") or ""

        # Get remaining percentage (-1 means unknown/not read by AMS)
        remain = int(tray_data.get("remain", -1))

        return AMSTray(
            ams_id=ams_id,
            tray_id=int(tray_data.get("id", 0)),
            tray_type=tray_type.strip(),
            tray_sub_brands=tray_sub_brands.strip(),
            tray_color=tray_color,
            remain=remain,
            tag_uid=tag_uid,
            tray_uuid=tray_uuid,
            tray_info_idx=tray_info_idx.strip(),
            tray_weight=int(tray_data.get("tray_weight", 1000)),
        )

    def convert_ams_slot_to_location(self, ams_id: int, tray_id: int) -> str:
        """Return a human-readable location string (e.g. "AMS A1") for the given AMS slot."""
        if ams_id >= 254:
            return "External Spool"

        if 128 <= ams_id <= 135:
            # AMS-HT units use IDs 128-135
            ht_letter = chr(ord("A") + (ams_id - 128))
            return f"AMS-HT {ht_letter}{tray_id + 1}"

        ams_letter = chr(ord("A") + ams_id)
        return f"AMS {ams_letter}{tray_id + 1}"

    def is_bambu_lab_spool(self, tray_uuid: str, tag_uid: str = "", tray_info_idx: str = "") -> bool:
        """Return True if tray_uuid or tag_uid identifies a Bambu Lab spool; tray_info_idx is ignored."""
        # Check tray_uuid (preferred - consistent across printer models)
        if tray_uuid:
            uuid = tray_uuid.strip()
            if len(uuid) == 32 and uuid != "00000000000000000000000000000000":
                try:
                    int(uuid, 16)
                    return True
                except ValueError:
                    pass

        # Fallback: check tag_uid (RFID tag - varies between printer readers)
        # Bambu Lab RFID tags are 16 hex characters (8 bytes)
        if tag_uid:
            tag = tag_uid.strip()
            if len(tag) == 16 and tag != "0000000000000000":
                try:
                    int(tag, 16)
                    logger.debug("Identified Bambu Lab spool via tag_uid fallback: %s", tag)
                    return True
                except ValueError:
                    pass

        return False

    def calculate_remaining_weight(self, remain_percent: int, spool_weight: int) -> float:
        """Return remaining filament weight in grams given a percentage and total spool weight."""
        return (remain_percent / 100.0) * spool_weight

    async def sync_ams_tray(
        self,
        tray: AMSTray,
        printer_name: str,
        disable_weight_sync: bool = False,
        cached_spools: list[dict] | None = None,
        inventory_remaining: float | None = None,
        spoolman_spool_id_hint: int | None = None,
        auto_add_unknown_rfid: bool = True,
        db=None,
    ) -> dict | None:
        """Sync one AMS tray to Spoolman; creates the spool on first sight, updates weight otherwise.

        ``db`` is a session for the colour catalogue: with it a NEW Bambu roll is
        matched to the filament of its own product line and its catalogue
        colour name is stored on the spool (upstream cb924e5c4). Without one
        the catalogue cannot be asked and the pick is material + colour alone.
        """
        logger.debug(
            f"Processing {printer_name} AMS {tray.ams_id} tray {tray.tray_id}: "
            f"type={tray.tray_type}, idx={tray.tray_info_idx or 'none'}, "
            f"uuid={tray.tray_uuid[:16] if tray.tray_uuid else 'none'}, "
            f"tag={tray.tag_uid[:8] if tray.tag_uid else 'none'}..."
        )

        # Determine which identifier to use for Spoolman (prefer tray_uuid, fallback to tag_uid)
        # Zero-filled values mean the AMS hasn't read the RFID tag — treat as no tag
        zero_uuid = "00000000000000000000000000000000"
        zero_tag = "0000000000000000"
        spool_tag = None
        if tray.tray_uuid and tray.tray_uuid != zero_uuid:
            spool_tag = tray.tray_uuid
        elif tray.tag_uid and tray.tag_uid != zero_tag:
            spool_tag = tray.tag_uid

        # Calculate remaining weight
        # Primary: AMS MQTT data (remain percentage + tray_weight)
        # Fallback: Built-in inventory tracked weight (when firmware sends invalid remain/tray_weight)
        if tray.remain >= 0 and tray.tray_weight > 0:
            remaining = self.calculate_remaining_weight(tray.remain, tray.tray_weight)
        elif inventory_remaining is not None:
            remaining = inventory_remaining
            logger.debug(
                "Using inventory weight fallback for %s AMS %s tray %s: %.1fg",
                printer_name,
                tray.ams_id,
                tray.tray_id,
                remaining,
            )
        else:
            remaining = None

        if spool_tag:
            # Primary path: match by RFID tag
            existing = await self.find_spool_by_tag(spool_tag, cached_spools=cached_spools)
            is_bambu = self.is_bambu_lab_spool(tray.tray_uuid, tray.tag_uid, tray.tray_info_idx)
            tag_format = "bambu" if is_bambu else None
            has_chip = bool(tray.tag_uid) and tray.tag_uid != zero_tag
            chip_uid = ams_chip_uid(tray.tag_uid, is_bambu) if has_chip else ""
            # A spool linked by its chip alone — by a reader, in Spoolman's own
            # UI — is found by that too: a reader stores the chip's own uid, a
            # link made from the AMS the padded one.
            for uid in dict.fromkeys((chip_uid, tray.tag_uid if has_chip else "")):
                if existing is None and uid and uid != spool_tag:
                    existing = await self.find_spool_by_tag(uid, cached_spools=cached_spools)
            # What the AMS reads of this reel: the tray UUID and the chip's uid.
            reel_uids = [tray.tray_uuid if tray.tray_uuid != zero_uuid else "", chip_uid]
            if existing:
                logger.info("Updating existing spool %s for tag %s...", existing["id"], spool_tag[:16])
                await self.add_native_tags(existing, reel_uids, tag_format)
                # Only a Bambu roll's tray names its own filament (as upstream):
                # under any other tag the preset is what the slot was last told.
                if is_bambu:
                    await self._fill_missing_tray_preset(existing, tray)
                return await self.update_spool(
                    spool_id=existing["id"],
                    remaining_weight=None if disable_weight_sync else remaining,
                )

            # Spool not found by tag - auto-create it, unless the user has
            # opted out of auto-adding unknown RFIDs (settings.auto_add_unknown_rfid).
            # Caller broadcasts unknown_tag on the resulting None so the UI can
            # surface a "+ Add to inventory" affordance on the slot.
            if not auto_add_unknown_rfid:
                logger.info(
                    "Auto-add disabled; skipping Spoolman spool create for %s (tag: %s...)",
                    tray.tray_sub_brands,
                    spool_tag[:16],
                )
                return None

            # A reel found to belong to a spool the list hides is left alone for
            # a while: the sync runs on every AMS update.
            known_at = self._known_reels.get(spool_tag.upper())
            if known_at is not None and time.monotonic() - known_at < TAG_REFUSAL_RETRY_SECONDS:
                return None
            holder = await self._holder_outside_the_list([spool_tag, *reel_uids, tray.tag_uid if has_chip else ""])
            if holder is not None:
                self._known_reels[spool_tag.upper()] = time.monotonic()
                logger.warning(
                    "Tag %s... already belongs to %sspool %s in Spoolman — no new spool created",
                    spool_tag[:16],
                    "archived " if holder.get("archived") else "",
                    holder.get("id"),
                )
                return None

            logger.info("Creating new spool in Spoolman for %s (tag: %s...)", tray.tray_sub_brands, spool_tag[:16])
            catalog_color = _COLOUR_NOT_RESOLVED
            if is_bambu:
                if db is not None:
                    catalog_color = await resolve_bambu_color(
                        db, tray.tray_color, tray_product_line(tray.tray_sub_brands, tray.tray_info_idx)
                    )
                filament = await self._find_or_create_filament(tray, catalog_color)
                filament_id = filament["id"] if filament else None
            else:
                # Non-BL spool with custom RFID: use generic vendor lookup
                brand = tray.tray_sub_brands if tray.tray_sub_brands != tray.tray_type else None
                try:
                    filament_id = await self.find_or_create_filament(
                        material=tray.tray_type,
                        subtype="",
                        brand=brand,
                        color_hex=tray.tray_color,
                        label_weight=tray.tray_weight,
                    )
                except (SpoolmanNotFoundError, SpoolmanUnavailableError, SpoolmanClientError):
                    logger.warning("Could not find or create filament for non-BL spool %s", tray.tray_sub_brands)
                    return None

            if not filament_id:
                logger.error("Failed to find or create filament for %s", tray.tray_sub_brands)
                return None

            import json

            created = await self.create_spool(
                filament_id=filament_id,
                remaining_weight=remaining,
                comment="Created by BamDude",
                extra={
                    "tag": json.dumps(spool_tag),
                    **(_tray_preset_extra(tray) if is_bambu else {}),
                    # A Spoolman filament has no colour name; ours lives on the
                    # spool, where the mapper reads it first. Without it the
                    # list fell back to a hex-only lookup that cannot tell
                    # Jade White (Basic) from Ivory White (Matte).
                    **(
                        {"bambu_color_name": json.dumps(catalog_color.name)}
                        if isinstance(catalog_color, CatalogColor)
                        else {}
                    ),
                },
            )
            # extra.tag stays written beside the native tags: an older Spoolman
            # reads nothing else, and a downgrade loses nothing.
            await self.add_native_tags(created, reel_uids, tag_format)
            return created

        # No-RFID fallback: use the spool ID resolved from the local slot-assignment table.
        # Never create new spools without a tag to avoid duplicates.
        if spoolman_spool_id_hint is not None:
            existing = next((s for s in (cached_spools or []) if s.get("id") == spoolman_spool_id_hint), None)
            if existing is None:
                try:
                    existing = await self.get_spool(spoolman_spool_id_hint)
                except (SpoolmanNotFoundError, SpoolmanUnavailableError):
                    existing = None
            if existing:
                logger.info(
                    "Updating spool %s by slot-assignment hint (no RFID tag available)",
                    existing["id"],
                )
                return await self.update_spool(
                    spool_id=existing["id"],
                    remaining_weight=None if disable_weight_sync else remaining,
                )

        logger.info(
            "%s AMS %s tray %s — skipping (no RFID tag and no slot-assignment hint)",
            printer_name,
            tray.ams_id,
            tray.tray_id,
        )
        return None

    async def _fill_missing_tray_preset(self, spool: dict, tray: AMSTray) -> None:
        """Give a tag-matched spool the tray's filament preset when it has none.

        Once: a preset already set — by this, or by hand in the spool form —
        stays. Best-effort; a refusal must not break the sync it rides on.
        """
        preset = _tray_preset_extra(tray)
        if not preset or ((spool.get("extra") or {}).get("bambu_slicer_filament") or "").strip('"'):
            return
        try:
            await self.merge_spool_extra(spool["id"], preset)
        except (SpoolmanNotFoundError, SpoolmanUnavailableError, SpoolmanClientError) as exc:
            logger.info("Could not store the tray's preset on Spoolman spool %s: %s", spool.get("id"), exc)

    async def _find_or_create_filament(self, tray: AMSTray, catalog_color=_COLOUR_NOT_RESOLVED) -> dict | None:
        """Return the Bambu Lab filament for this tray's product line, creating it if absent.

        Material plus colour is not an identity: PLA Basic Black and PLA Matte
        Charcoal are both PLA at ``000000``, so a Matte roll was hung on the
        Basic filament and read as Basic everywhere (upstream cb924e5c4, #2907).
        The line is in the filament's NAME, ranked by ``product_line_rank``.

        ``catalog_color`` is what the colour catalogue says about the roll —
        ``None`` when it has no row — resolved by the caller so this client stays
        HTTP-only. A caller that asked no catalogue at all (no session) leaves
        it out and gets the pick as it was: the first material + colour match.
        """
        resolved = catalog_color is not _COLOUR_NOT_RESOLVED
        color_name = catalog_color.name if isinstance(catalog_color, CatalogColor) else None
        product_line = tray_product_line(tray.tray_sub_brands, tray.tray_info_idx)
        bambu_vendor_id = await self.ensure_bambu_vendor()
        material_upper = tray.tray_type.upper()
        # The match key is the stored shape (#2912): an opaque tray still finds
        # the six-character filaments every existing instance is full of, a
        # clear tray keys to eight and gets its own record.
        color = color_match_key(tray.tray_color)

        # Search internal filaments — only match Bambu Lab vendor.
        filaments = await self.get_filaments()
        same_colour: list[dict] = []
        for filament in filaments:
            fil_vendor_id = filament.get("vendor_id") or filament.get("vendor", {}).get("id")
            if fil_vendor_id != bambu_vendor_id:
                continue
            fil_material = filament.get("material") or ""
            if fil_material.upper() == material_upper and color_match_key(filament.get("color_hex")) == color:
                if not resolved:
                    return filament
                same_colour.append(filament)
        existing = best_product_line_match(same_colour, color_name, product_line, tray.tray_type)
        if existing is not None:
            return existing

        # Search external filaments (SpoolmanDB) — restrict to Bambu Lab.
        # The ``/api/v1/external/filament`` endpoint returns the full multi-
        # vendor catalogue with no server-side filter, so without a
        # manufacturer check the first PLA / black hit is typically 3DJAKE
        # or 3DXTECH (catalogue is roughly ID-sorted and Bambu's entries are
        # mid-list). Bambu Lab RFID spools were being silently labelled
        # with competitor product names. Upstream Bambuddy #1330 / commit
        # a1d6fb22.
        external = await self.get_external_filaments()
        sub_brand = (tray.tray_sub_brands or "").strip().lower()
        bambu_candidates: list[dict] = []
        for filament in external:
            manufacturer = (filament.get("manufacturer") or "").strip().lower()
            ext_id = (filament.get("id") or "").strip().lower()
            # ``id.startswith("bambulab_")`` is a defensive fallback for
            # SpoolmanDB schema drift (some entries lack ``manufacturer``).
            if manufacturer != "bambu lab" and not ext_id.startswith("bambulab_"):
                continue
            fil_material = filament.get("material") or ""
            if fil_material.upper() == material_upper and color_match_key(filament.get("color_hex")) == color:
                bambu_candidates.append(filament)

        if resolved:
            # The library folds the line into the name ("Matte Charcoal") or
            # gives the bare colour ("Black"); the same ranking picks the entry
            # of the roll's own line. No ranked entry means the library cannot
            # say what this roll is — it is then built from what the printer
            # reported, below, never from whichever candidate came first.
            chosen = best_product_line_match(bambu_candidates, color_name, product_line, tray.tray_type)
            if chosen is not None:
                return await self._create_filament_from_external(chosen, tray)
        elif bambu_candidates:
            # Prefer the entry whose ``name`` matches the AMS
            # ``tray_sub_brands`` (e.g. "PLA Basic", "Support for PLA/PETG
            # Black") so the more specific variant wins over a generic
            # "Black" entry when both are present.
            chosen = next(
                (f for f in bambu_candidates if (f.get("name") or "").strip().lower() == sub_brand),
                bambu_candidates[0],
            )
            return await self._create_filament_from_external(chosen, tray)

        # Not found in either source — create a new Bambu Lab filament from
        # scratch.
        return await self.create_filament(
            name=product_line or tray.tray_type,
            vendor_id=bambu_vendor_id,
            material=tray.tray_type,
            color_hex=color,
            weight=tray.tray_weight,
        )

    async def _create_filament_from_external(self, external: dict, tray: AMSTray) -> dict | None:
        """Create an internal Spoolman filament from an external library entry.

        The entry supplies what the printer cannot (density, label weight); the
        NAME is the tray's product line, as for a filament built from scratch,
        so both read the same subtype. The library's own name ("Matte
        Charcoal") is a colour with the line folded in, and the colour name is
        stored on the spool instead.
        """
        vendor_id = await self.ensure_bambu_vendor()
        return await self.create_filament(
            name=tray_product_line(tray.tray_sub_brands, tray.tray_info_idx) or external.get("name") or tray.tray_type,
            vendor_id=vendor_id,
            material=external.get("material", tray.tray_type),
            # `or`, not a two-argument get: an entry carrying the key with an
            # explicit null must still reach the tray fallback.
            color_hex=external.get("color_hex") or color_match_key(tray.tray_color),
            weight=external.get("weight", tray.tray_weight),
            # Forward density so it isn't overwritten by the PLA-default
            # 1.24 inside create_filament (#1330).
            density=external.get("density"),
        )


def _tray_preset_extra(tray: AMSTray) -> dict:
    """The tray's filament preset as Spoolman spool extras (JSON-encoded), or nothing.

    ``bambu_slicer_filament`` is what the slot plan reads when no calibration
    is linked (upstream 5e3a2f278, issue #3433): without it a Bambu spool the
    sync created was configured as the generic of its material on any slot
    without a reader. The name comes from our family catalogue, the tray's own
    wording otherwise.
    """
    from backend.app.utils import filament_catalog

    preset_id = (tray.tray_info_idx or "").strip()
    if not preset_id:
        return {}
    family = filament_catalog.get_family(preset_id)
    name = (family.alias if family else "") or (tray.tray_sub_brands or "").strip()
    extra = {"bambu_slicer_filament": json.dumps(preset_id)}
    if name:
        extra["bambu_slicer_filament_name"] = json.dumps(name)
    return extra


class ArchivedTagIndex:
    """Answers "is this tray an ARCHIVED spool's reel?" for one AMS sync.

    The sync cache (``get_spools`` → ``GET /spool``) never lists archived
    spools — Spoolman returns them only with ``allow_archived`` — so to
    ``find_spool_by_tag`` a retired reel still sitting in the AMS looks
    brand new, and the sync would auto-create a duplicate or prompt "+ Add".
    That is the usual state right after an AMS auto-switch, when the runout
    close-out archives the emptied reel while it stays in its slot.

    One extra request at most per sync, and only once a tag has missed the
    active cache. Best-effort: when archived spools cannot be read, nothing
    is treated as archived and the sync behaves as it always did.
    """

    def __init__(self, client: SpoolmanClient) -> None:
        self._client = client
        self._archived_tags: set[str] | None = None

    async def holds_archived_reel(self, spool_tag: str, cached_spools: list[dict], tag_uid: str = "") -> bool:
        # Native tags count as much as extra.tag: an archived spool tagged in
        # Spoolman itself is the same known reel — and so is one that holds the
        # reel by its chip alone (``tag_uid``: the AMS's reading and the chip's
        # own form of it), as a reader or Spoolman's UI links it.
        chip = normalize_tag_uid(tag_uid)
        wanted = {spool_tag.strip('"').upper()}
        if is_linkable_tag_uid(chip):
            wanted |= {chip, ams_chip_uid(chip, True)}
        wanted.discard("")
        if not wanted or any(wanted & spool_tag_uids(s) for s in cached_spools):
            return False
        if self._archived_tags is None:
            try:
                spools = await self._client.get_all_spools(allow_archived=True)
            except Exception as exc:  # noqa: BLE001 — a guard must never break the sync it guards
                logger.debug("Could not list archived Spoolman spools for the AMS sync: %s", exc)
                spools = []
            self._archived_tags = {tag for s in spools if s.get("archived") for tag in spool_tag_uids(s)}
        return bool(wanted & self._archived_tags)


# Global client instance (initialized when settings are loaded)
_spoolman_client: SpoolmanClient | None = None


async def get_spoolman_client() -> SpoolmanClient | None:
    """Return the global SpoolmanClient, or None if not configured."""
    return _spoolman_client


async def init_spoolman_client(url: str) -> SpoolmanClient:
    """Initialise (or reinitialise) the global SpoolmanClient; raises ValueError if url fails SSRF guard."""
    from backend.app.api.routes._spoolman_helpers import assert_safe_spoolman_url

    assert_safe_spoolman_url(url)

    global _spoolman_client
    if _spoolman_client:
        await _spoolman_client.close()

    _spoolman_client = SpoolmanClient(url)
    return _spoolman_client


async def close_spoolman_client():
    """Close the global Spoolman client."""
    global _spoolman_client
    if _spoolman_client:
        await _spoolman_client.close()
        _spoolman_client = None
