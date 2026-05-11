"""Helpers for building ``LibraryFileMakerworldMeta`` rows.

Shared between the live import flow (``routes/makerworld.py::import_instance``)
and the m056 backfill, so both paths produce identical rows. Splitting it
out also lets future CLI re-backfill scripts call the same logic.

Cover download writes JPEGs/PNGs to ``<archive_dir>/library/makerworld-covers/``
named by ``<library_file_id>-cover.<ext>`` and ``<library_file_id>-variant.<ext>``.
Path stored on the meta row is relative to ``settings.base_dir`` — same
shape as ``library_files.thumbnail_path`` so existing path resolvers work.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from backend.app.core.config import settings
from backend.app.services.makerworld import MakerWorldError, MakerWorldService

logger = logging.getLogger(__name__)


_COVER_EXT_FROM_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def get_makerworld_covers_dir() -> Path:
    """Resolve (and create) the on-disk directory for downloaded covers."""
    covers_dir = Path(settings.archive_dir) / "library" / "makerworld-covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    return covers_dir


def _to_relative_path(absolute_path: Path) -> str:
    """Same contract as ``routes/library.py::to_relative_path`` but local
    so this module doesn't pull a routes-layer dependency.
    """
    base_dir = Path(settings.base_dir)
    try:
        return str(absolute_path.relative_to(base_dir))
    except ValueError:
        return str(absolute_path)


def _author_profile_url(creator: dict[str, Any] | None) -> str | None:
    """Best-effort MakerWorld profile URL from a ``designCreator`` payload.

    MakerWorld's response shape varies — handle takes precedence (cleaner
    URLs) and ``uid`` is the numeric fallback. Returns None when we can't
    build anything useful rather than guessing.
    """
    if not isinstance(creator, dict):
        return None
    handle = creator.get("handle") if isinstance(creator.get("handle"), str) else None
    if handle:
        return f"https://makerworld.com/en/@{handle}"
    uid = creator.get("uid")
    if isinstance(uid, int) and uid > 0:
        return f"https://makerworld.com/en/u/{uid}"
    return None


def _pick_instance(design: dict[str, Any], instances: list[dict[str, Any]], profile_id: int | None) -> dict[str, Any]:
    """Pick the right instance for ``profile_id`` — preferring the
    ``design.instances`` shape (richer; carries ``extention.modelInfo``)
    and falling back to the ``/instances`` hits.
    """
    design_insts = design.get("instances") if isinstance(design.get("instances"), list) else []
    if profile_id is not None:
        for inst in design_insts:
            if isinstance(inst, dict) and inst.get("profileId") == profile_id:
                return inst
        for inst in instances:
            if isinstance(inst, dict) and inst.get("profileId") == profile_id:
                return inst
    # First-available fallback (matches the import-route default)
    for inst in design_insts:
        if isinstance(inst, dict) and isinstance(inst.get("profileId"), int):
            return inst
    for inst in instances:
        if isinstance(inst, dict) and isinstance(inst.get("profileId"), int):
            return inst
    return {}


def _compat_pair(instance: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Extract ``(sliced_for, compatible_models)``.

    Source of truth is the per-instance ``extention.modelInfo`` block
    (merged into ``/instances`` hits by the resolve route + present on
    ``design.instances`` directly). ``sliced_for`` is the primary
    ``compatibility`` entry; ``compatible_models`` is the union (primary
    first, no duplicates).
    """
    ext = (instance.get("extention") or {}).get("modelInfo") or {}
    primary = ext.get("compatibility") if isinstance(ext.get("compatibility"), str) else None
    others_raw = ext.get("otherCompatibility")
    others = [str(o) for o in others_raw if isinstance(o, str)] if isinstance(others_raw, list) else []
    if primary and primary not in others:
        compatible = [primary, *others]
    else:
        compatible = others
    return primary, compatible


def _materials(instance: dict[str, Any]) -> tuple[int | None, list[dict[str, Any]] | None]:
    """Pull ``(material_count, materials[])`` from an instance.

    MakerWorld exposes ``materialCnt`` (int) and ``instanceFilaments``
    (list of dicts with material info). We keep the raw filament dicts
    verbatim because the shape is undocumented and we'd rather not
    drop fields the frontend may want.
    """
    cnt = instance.get("materialCnt")
    cnt_val = int(cnt) if isinstance(cnt, int) else None
    filaments_raw = instance.get("instanceFilaments")
    materials = filaments_raw if isinstance(filaments_raw, list) else None
    return cnt_val, materials


def build_meta_dict(
    *,
    library_file_id: int,
    design: dict[str, Any],
    instances: list[dict[str, Any]],
    profile_id: int | None,
    variant_url: str | None,
    model_id_alphanumeric: str | None,
) -> dict[str, Any]:
    """Project the design + instances payload into ``LibraryFileMakerworldMeta`` kwargs.

    Pure function (no I/O). The caller wires the result into a
    ``LibraryFileMakerworldMeta`` constructor and fills ``cover_path`` /
    ``variant_cover_path`` separately via ``download_covers`` below.
    """
    instance = _pick_instance(design, instances, profile_id)
    sliced_for, compatible_models = _compat_pair(instance)
    material_count, materials = _materials(instance)
    creator = design.get("designCreator") if isinstance(design.get("designCreator"), dict) else None
    instance_creator = instance.get("instanceCreator") if isinstance(instance.get("instanceCreator"), dict) else None
    # Prefer the instance-level creator when present (some MakerWorld
    # uploads have collaborator instance authors distinct from the design
    # author); fall back to the design-level creator otherwise.
    effective_creator = instance_creator or creator
    author_name = (
        effective_creator.get("name")
        if isinstance(effective_creator, dict) and isinstance(effective_creator.get("name"), str)
        else None
    )

    original_design_id_raw = design.get("originalDesignId")
    original_design_id = int(original_design_id_raw) if isinstance(original_design_id_raw, int) else None

    needs_ams_raw = instance.get("needAms")
    needs_ams = bool(needs_ams_raw) if isinstance(needs_ams_raw, bool) else None

    return {
        "library_file_id": library_file_id,
        "title": design.get("title") if isinstance(design.get("title"), str) else None,
        "description": design.get("summary") if isinstance(design.get("summary"), str) else None,
        "author_name": author_name,
        "author_profile_url": _author_profile_url(effective_creator),
        "license": design.get("license") if isinstance(design.get("license"), str) else None,
        "original_design_id": original_design_id,
        "variant_title": instance.get("title") if isinstance(instance.get("title"), str) else None,
        "variant_description": (instance.get("description") if isinstance(instance.get("description"), str) else None),
        "variant_url": variant_url,
        "profile_id": profile_id,
        "sliced_for": sliced_for,
        "compatible_models": compatible_models or None,
        "needs_ams": needs_ams,
        "material_count": material_count,
        "materials": materials,
        "model_id_alphanumeric": model_id_alphanumeric,
        "raw_payload": {
            "design": design,
            "instance": instance,
        },
    }


def _extension_for(content_type: str | None) -> str:
    """Pick a sensible file extension for a downloaded cover."""
    if isinstance(content_type, str):
        ct = content_type.split(";")[0].strip().lower()
        if ct in _COVER_EXT_FROM_MIME:
            return _COVER_EXT_FROM_MIME[ct]
    return ".jpg"  # safe default — MakerWorld CDN serves JPEGs by default


async def _download_one(service: MakerWorldService, url: str, dest_no_ext: Path) -> str | None:
    """Fetch a single cover and write it to disk under ``dest_no_ext.<ext>``.

    Returns the absolute path of the written file, or None on any error
    (network, SSRF guard, etc.). Errors are swallowed — covers are
    nice-to-have and shouldn't tank the import or backfill.
    """
    try:
        data, content_type = await service.fetch_thumbnail(url)
    except MakerWorldError as exc:
        logger.warning("MakerWorld cover download failed (%s): %s", url, exc)
        return None
    except Exception as exc:
        logger.warning("MakerWorld cover download crashed (%s): %s", url, exc)
        return None
    ext = _extension_for(content_type)
    dest = dest_no_ext.with_suffix(ext)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        logger.warning("Failed to write MakerWorld cover to %s: %s", dest, exc)
        return None
    return str(dest)


async def download_covers(
    service: MakerWorldService,
    *,
    library_file_id: int,
    cover_url: str | None,
    variant_cover_url: str | None,
) -> tuple[str | None, str | None]:
    """Download model + variant covers; return ``(cover_rel, variant_rel)``
    as paths relative to ``settings.base_dir`` (None if not downloaded).

    Existing cover files for the same library_file_id are overwritten,
    which makes a m056 re-run idempotent.
    """
    covers_dir = get_makerworld_covers_dir()
    cover_rel: str | None = None
    variant_cover_rel: str | None = None

    # Wipe any prior covers for this library_file_id so a re-import or
    # re-backfill doesn't leave stale files of a different extension.
    for path in covers_dir.glob(f"{library_file_id}-cover.*"):
        try:
            path.unlink()
        except OSError:
            pass
    for path in covers_dir.glob(f"{library_file_id}-variant.*"):
        try:
            path.unlink()
        except OSError:
            pass

    if cover_url:
        abs_path = await _download_one(service, cover_url, covers_dir / f"{library_file_id}-cover")
        if abs_path:
            cover_rel = _to_relative_path(Path(abs_path))

    if variant_cover_url:
        abs_path = await _download_one(service, variant_cover_url, covers_dir / f"{library_file_id}-variant")
        if abs_path:
            variant_cover_rel = _to_relative_path(Path(abs_path))

    return cover_rel, variant_cover_rel


def cleanup_cover_files(library_file_id: int) -> None:
    """Delete any on-disk MakerWorld cover files for ``library_file_id``.

    Called from the library-file hard-delete path so the FK-CASCADE on
    the meta row is matched by an on-disk wipe. Safe to call when no
    files exist (no-op).
    """
    try:
        covers_dir = get_makerworld_covers_dir()
    except OSError as exc:
        logger.debug("MakerWorld covers dir not present, skipping cleanup: %s", exc)
        return
    for pattern in (f"{library_file_id}-cover.*", f"{library_file_id}-variant.*"):
        for path in covers_dir.glob(pattern):
            try:
                os.unlink(path)
            except OSError as exc:
                logger.debug("Failed to unlink MakerWorld cover %s: %s", path, exc)
