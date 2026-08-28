"""Preview-slice cache for the SliceModal.

The slice modal needs the per-plate filament list before the user picks
profiles. For sliced files this lives in ``Metadata/slice_info.config`` and
the ``/filament-requirements`` endpoint can read it directly. For unsliced
project files it doesn't exist yet — only the slicer can produce it, since
Bambu Studio applies its own pruning to painted-face data at slice time.

This module wraps the sidecar's ``slice_without_profiles`` call so the
endpoint can run a preview slice with the project's embedded settings,
parse the result's slice_info, and return the actual filament list. Results
are cached by ``(kind, source_id, plate_id, content_hash)`` so repeat
opens of the modal on the same plate are instant; LRU eviction keeps the
cache bounded. Hash invalidation handles in-place file replacement; no TTL
is used because preview-slice output is deterministic for a given file
content.

⚠️ The one thing that can defeat those embedded settings is a custom G-code
template written by a Bambu Studio newer than the sidecar: it fails to parse
before any slice_info exists. That case gets ONE retry with the offending
template blanked — see ``_blank_custom_gcode``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import zipfile
from collections import OrderedDict
from io import BytesIO

import defusedxml.ElementTree as ET

from backend.app.services.slicer_api import (
    SlicerApiError,
    SlicerApiService,
)

logger = logging.getLogger(__name__)

_PREVIEW_CACHE_MAX = 256
# Cache values: list[dict] on success, [] on parsed-but-empty (slicer
# returned a 3MF without filament data for this plate — caching the negative
# avoids burning 30s+ per modal open on a known-bad input).
_preview_cache: OrderedDict[tuple[str, int, int, str], list[dict]] = OrderedDict()
# Per-key locks prevent N concurrent modal opens on the same (file, plate)
# from launching N redundant preview slices — only the first one runs, the
# rest wait and read from the cache. Locks are evicted alongside cache
# entries to keep the dict bounded; transient sidecar failures are NOT
# cached so they retry naturally on next request.
_preview_locks: dict[tuple[str, int, int, str], asyncio.Lock] = {}


_PROJECT_SETTINGS_PATH = "Metadata/project_settings.config"

# The slicer names the offending G-code field in its stderr, e.g.
#   timelapse_gcode Parsing error at line 13: Not a variable name
#       {if timelapse_inline_photo}
_GCODE_PARSE_ERROR_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+Parsing error at line \d+:",
    re.MULTILINE,
)

# Custom G-code fields we are willing to blank to get a preview through.
#
# ⚠️ Deliberately narrow, and the narrowness IS the point: blanking a field that
# EXTRUDES would change the very numbers the preview exists to report.
# ``machine_start_gcode`` lays a prime line, ``change_filament_gcode`` purges —
# silence either and the returned grams are quietly wrong, which is worse than
# returning nothing at all. Everything below only moves the toolhead or emits
# markers, so removing it cannot alter filament accounting.
#
# Keys are normalised (see ``_normalise_option``) because the slicer reports
# ``timelapse_gcode`` while the 3MF stores ``time_lapse_gcode``.
_BLANKABLE_GCODE_FIELDS = frozenset(
    {
        "timelapsegcode",
        "layerchangegcode",
        "beforelayerchangegcode",
        "machinepausegcode",
        "templatecustomgcode",
        "printingbyobjectgcode",
    }
)


def _normalise_option(name: str) -> str:
    """Fold a config-option name to a comparable form.

    ⚠️ Bambu Studio's error text and its own 3MF config disagree on word breaks
    for the same option (``timelapse_gcode`` vs ``time_lapse_gcode``), so
    matching the literal string silently fails to find the field it just named.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _unparsable_gcode_option(error_text: str) -> str | None:
    """The normalised name of the custom-G-code field the slicer choked on.

    Returns None when the failure was something else entirely, or when the named
    field is one whose removal could change filament accounting. Callers read
    None as "do not retry".
    """
    match = _GCODE_PARSE_ERROR_RE.search(error_text)
    if match is None:
        return None
    option = _normalise_option(match.group(1))
    return option if option in _BLANKABLE_GCODE_FIELDS else None


def _blank_custom_gcode(file_bytes: bytes, option: str) -> bytes | None:
    """A copy of the 3MF with ``option``'s G-code template emptied, or None.

    A 3MF saved by a newer Bambu Studio can carry a machine G-code template that
    references a config variable an older sidecar does not define — Studio 2.8
    writes ``{if timelapse_inline_photo}`` into ``time_lapse_gcode`` without
    exporting a definition for it, so the template is unresolvable the moment it
    leaves Studio. Slicing then dies on a placeholder parse error before
    producing any slice_info, and the preview has nothing to read.

    ⚠️ Emptying just the ONE named template is what keeps the answer honest:
    overriding the process preset instead discards the project's own support
    configuration, which loses whole slots and moves the reported grams. The
    file's own process settings, supports and per-slot assignments all survive.

    Returns None when there is nothing to do — not a 3MF, no embedded settings,
    no matching field, or a field already empty — so the caller can skip a retry
    that would fail identically.
    """
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
            if _PROJECT_SETTINGS_PATH not in zf.namelist():
                return None
            entries = [(info, zf.read(info.filename)) for info in zf.infolist()]
            settings = json.loads(zf.read(_PROJECT_SETTINGS_PATH).decode("utf-8", "replace"))
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(settings, dict):
        return None

    # Matched on the normalised name so the slicer's spelling finds the
    # config's. Only ``*_gcode`` keys are eligible, so a same-stem non-template
    # setting can never be caught by the fold.
    blanked: list[str] = []
    for key, value in settings.items():
        if not key.endswith("_gcode") or _normalise_option(key) != option:
            continue
        if isinstance(value, str) and value:
            settings[key] = ""
        elif isinstance(value, list) and any(value):
            # ⚠️ Preserve the container type — a per-extruder template is a
            # list, and handing the CLI a bare string where it expects one
            # trades this parse error for a different one.
            settings[key] = [""] * len(value)
        else:
            continue
        blanked.append(key)
    if not blanked:
        return None

    out = BytesIO()
    try:
        with zipfile.ZipFile(out, "w") as zf_out:
            for info, data in entries:
                if info.filename == _PROJECT_SETTINGS_PATH:
                    data = json.dumps(settings, indent=4).encode("utf-8")
                # Carry each member's original compression across, so the copy
                # stays a 3MF the slicer reads the same way as the original.
                zf_out.writestr(info, data, compress_type=info.compress_type)
    except (OSError, ValueError):
        return None
    logger.debug("Preview slice: emptied custom G-code field(s) %s for retry", ", ".join(blanked))
    return out.getvalue()


def _content_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()[:16]


async def get_preview_filaments(
    *,
    kind: str,
    source_id: int,
    plate_id: int,
    file_bytes: bytes,
    file_name: str,
    api_url: str,
    request_id: str | None = None,
    timeout_seconds: float | None = None,
) -> list[dict] | None:
    """Run a preview slice for ``plate_id`` using the file's embedded settings,
    parse the resulting slice_info, and return the per-plate filament list.

    Returns ``None`` when the preview slice fails — the caller falls back
    to whatever heuristic it has (typically the project_filaments list).
    """
    h = _content_hash(file_bytes)
    key = (kind, source_id, plate_id, h)
    cached = _preview_cache.get(key)
    if cached is not None:
        _preview_cache.move_to_end(key)
        return cached

    lock = _preview_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _preview_cache.get(key)
        if cached is not None:
            _preview_cache.move_to_end(key)
            return cached

        try:
            # A preview slice is bounded the same way a real one is (#2730):
            # a heavy plate can take a long time and must not be cut off while
            # the slicer is visibly working.
            svc_kwargs = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}

            async def _slice(model_bytes: bytes):
                async with SlicerApiService(base_url=api_url, **svc_kwargs) as svc:
                    return await svc.slice_without_profiles(
                        model_bytes=model_bytes,
                        model_filename=file_name,
                        plate=plate_id,
                        export_3mf=True,
                        request_id=request_id,
                    )

            result = await _slice(file_bytes)
        except SlicerApiError as e:
            # One retry, and only for a custom-G-code template the sidecar
            # cannot parse — a file from a Studio newer than the sidecar. The
            # alternative is to hand the caller nothing and let it fall back to
            # its painted-face heuristic, so a retry that reproduces the file's
            # own settings is strictly better than the status quo. Anything else
            # (unreachable sidecar, timeout, bad input) returns as before.
            #
            # ⚠️ Whether a retry is even possible is decided BEFORE anything is
            # logged, so a slice that recovers never announces itself as a
            # failure. Logging the first attempt at WARNING regardless sends a
            # reader looking for a bug in a path that fixed itself twenty
            # seconds later, several screens further down the log.
            retry_bytes = None
            option = _unparsable_gcode_option(str(e))
            if option is not None:
                retry_bytes = _blank_custom_gcode(file_bytes, option)
            if retry_bytes is None:
                logger.warning(
                    "Preview slice failed for %s/%s plate %s: %s",
                    kind,
                    source_id,
                    plate_id,
                    e,
                )
                return None
            logger.info(
                "Preview slice for %s/%s plate %s hit unparsable custom G-code; retrying without it. "
                "The file's G-code references a setting this slicer build does not know, so it is "
                "probably from a newer Bambu Studio than the sidecar. Original failure: %s",
                kind,
                source_id,
                plate_id,
                e,
            )
            try:
                result = await _slice(retry_bytes)
            except SlicerApiError as retry_exc:
                logger.warning(
                    "Preview slice retry without the unparsable G-code also failed for %s/%s plate %s: %s",
                    kind,
                    source_id,
                    plate_id,
                    retry_exc,
                )
                return None
            except Exception as retry_exc:  # noqa: BLE001 — never break the modal on sidecar issues
                logger.warning("Preview slice retry unexpected error: %s", retry_exc)
                return None
            logger.info("Preview slice for %s/%s plate %s succeeded on retry", kind, source_id, plate_id)
        except Exception as e:  # noqa: BLE001 — never break the modal on sidecar issues
            logger.warning("Preview slice unexpected error: %s", e)
            return None

        filaments = _parse_filaments_from_sliced_3mf(result.content, plate_id)
        # Negative-cache the parse failure: a slice that succeeds but yields
        # no parsable filament data for this plate is a deterministic
        # property of the input. Re-running produces the same result, just
        # N seconds slower. Empty list signals "preview was tried, no usable
        # data" so the caller can fall through.
        cache_value: list[dict] = filaments if filaments is not None else []
        _preview_cache[key] = cache_value
        if len(_preview_cache) > _PREVIEW_CACHE_MAX:
            evicted_key, _ = _preview_cache.popitem(last=False)
            _preview_locks.pop(evicted_key, None)
        return filaments


def _parse_filaments_from_sliced_3mf(content: bytes, plate_id: int) -> list[dict] | None:
    """Extract ``<filament>`` entries for ``plate_id`` from a sliced 3MF's
    Metadata/slice_info.config. Returns ``None`` on any parse error so the
    caller knows to fall back."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None
            data = zf.read("Metadata/slice_info.config").decode()
    except (zipfile.BadZipFile, OSError):
        return None

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    for plate_elem in root.findall(".//plate"):
        idx = None
        for meta in plate_elem.findall("metadata"):
            if meta.get("key") == "index":
                try:
                    idx = int(meta.get("value", ""))
                except (ValueError, TypeError):
                    pass
                break
        if idx != plate_id:
            continue
        out: list[dict] = []
        for f in plate_elem.findall("filament"):
            fid = f.get("id")
            if not fid:
                continue
            try:
                slot_id = int(fid)
            except (ValueError, TypeError):
                continue
            try:
                used_grams = float(f.get("used_g", "0"))
            except (ValueError, TypeError):
                used_grams = 0
            try:
                used_meters = float(f.get("used_m", "0"))
            except (ValueError, TypeError):
                used_meters = 0
            out.append(
                {
                    "slot_id": slot_id,
                    "type": f.get("type", ""),
                    "color": f.get("color", ""),
                    "used_grams": round(used_grams, 1),
                    "used_meters": used_meters,
                    "tray_info_idx": f.get("tray_info_idx", ""),
                },
            )
        return sorted(out, key=lambda x: x["slot_id"])
    return None
