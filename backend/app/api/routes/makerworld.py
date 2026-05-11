"""MakerWorld integration routes.

User pastes a MakerWorld URL → BamDude resolves it → shows plate list →
one-click import/print. The URL-paste flow covers the actual discovery
pattern (Reddit/YouTube/shared links) without needing to replicate
MakerWorld's whole search UI.

Search/browse endpoints are intentionally NOT exposed: the public-facing
``design/search`` endpoint returns empty results from server-originated
requests.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.cloud import get_stored_token
from backend.app.api.routes.library import save_3mf_bytes_to_library
from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta
from backend.app.models.user import User
from backend.app.schemas.library_file_makerworld_meta import LibraryFileMakerworldMetaResponse
from backend.app.schemas.makerworld import (
    MakerWorldAlreadyImportedEntry,
    MakerWorldImportRequest,
    MakerWorldImportResponse,
    MakerWorldImportsPage,
    MakerWorldImportsPaginationMeta,
    MakerWorldRecentImport,
    MakerWorldResolvedModel,
    MakerWorldResolveRequest,
    MakerWorldStatus,
)
from backend.app.services.makerworld import (
    MakerWorldAuthError,
    MakerWorldError,
    MakerWorldForbiddenError,
    MakerWorldNotFoundError,
    MakerWorldService,
    MakerWorldUnavailableError,
    MakerWorldUrlError,
)
from backend.app.services.makerworld_meta import build_meta_dict, download_covers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/makerworld", tags=["makerworld"])

_SOURCE_TYPE = "makerworld"


async def _build_service(db: AsyncSession, user: User | None) -> MakerWorldService:
    """Construct a per-request MakerWorldService seeded with the caller's
    stored Bambu Cloud bearer token when available.

    Mirrors ``cloud.build_authenticated_cloud`` — the token is entirely
    optional; anonymous calls (metadata, URL resolution) still work.
    """
    token, _email, _region = await get_stored_token(db, user)
    return MakerWorldService(auth_token=token)


def _canonical_url(model_id: int, profile_id: int | None = None) -> str:
    """Build a stable source_url we use for dedupe.

    Dedupe is keyed per *plate* (profile) rather than per model, since the
    ``/iot-service/.../profile/{profileId}`` download returns a specific
    plate — not the full multi-plate zip — so two different plates of the
    same design should become two separate library entries. Canonical
    shape uses the locale-free path with the ``#profileId-`` fragment so
    all URL variants of the same plate still collapse (e.g. ``/en/models/
    123-slug?from=search#profileId-456`` and ``/de/models/123#profileId-
    456`` both map to ``https://makerworld.com/models/123#profileId-
    456``). Plate-less imports (legacy or whole-design) keep the old
    model-only shape for backwards compatibility with existing rows.
    """
    if profile_id:
        return f"https://makerworld.com/models/{model_id}#profileId-{profile_id}"
    return f"https://makerworld.com/models/{model_id}"


def _map_service_error(exc: MakerWorldError) -> HTTPException:
    """Translate service exceptions into HTTP responses."""
    if isinstance(exc, MakerWorldUrlError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, MakerWorldAuthError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, MakerWorldForbiddenError):
        # 403 forwards MakerWorld's own refusal message (content-gated,
        # region-locked, requires points, etc.) — UI surfaces it verbatim.
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, MakerWorldNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, MakerWorldUnavailableError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=f"MakerWorld error: {exc}")


@router.get("/thumbnail")
async def proxy_thumbnail(
    url: str = Query(..., description="MakerWorld CDN image URL (makerworld.bblmw.com or public-cdn.bblmw.com)"),
):
    """Proxy a MakerWorld CDN thumbnail.

    The SPA's ``img-src`` CSP only allows ``'self' data: blob:`` — hotlinking
    from makerworld.bblmw.com is blocked. This endpoint refetches the image
    server-side and returns it with a long cache window.

    **Unauthenticated on purpose**: ``<img>`` tags can't send Authorization
    headers, so requiring a Bearer token here would break the whole feature
    (browsers would get 401 on every image, rendering as broken-image
    placeholders). The thumbnails being proxied are MakerWorld's *public*
    CDN — any visitor to makerworld.com can fetch them without auth — so no
    data is exposed. The SSRF guard inside ``fetch_thumbnail`` restricts
    the upstream host to the MakerWorld CDN allowlist, so this can't be
    abused as a generic open proxy. Whitelisted in ``auth_middleware`` so
    the always-on auth gate doesn't 401 the proxied image fetch.

    URLs are content-addressable (filename contains a hash), so the
    aggressive ``immutable`` cache-control is safe.
    """
    service = MakerWorldService()
    try:
        payload, content_type = await service.fetch_thumbnail(url)
    except MakerWorldError as exc:
        raise _map_service_error(exc) from exc
    finally:
        await service.close()

    return Response(
        content=payload,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


@router.get("/status", response_model=MakerWorldStatus)
async def get_status(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.MAKERWORLD_VIEW),
):
    """Report whether the caller can import 3MFs (needs a Bambu Cloud token)."""
    token, _email, _region = await get_stored_token(db, current_user)
    has_token = bool(token)
    return MakerWorldStatus(has_cloud_token=has_token, can_download=has_token)


@router.post("/resolve", response_model=MakerWorldResolvedModel)
async def resolve_url(
    body: MakerWorldResolveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.MAKERWORLD_VIEW),
):
    """Resolve a MakerWorld URL to full model metadata + plate list.

    The response also tells the caller which (if any) LibraryFile rows already
    exist for the same model URL, so the UI can show an "Already imported"
    badge and skip a redundant download.
    """
    try:
        model_id, profile_id = MakerWorldService.parse_url(body.url)
    except MakerWorldError as exc:
        raise _map_service_error(exc) from exc

    service = await _build_service(db, current_user)
    try:
        design = await service.get_design(model_id)
        instances_envelope = await service.get_design_instances(model_id)
    except MakerWorldError as exc:
        raise _map_service_error(exc) from exc
    finally:
        await service.close()

    # MakerWorld's instances payload is ``{"total": N, "hits": [...]}``; callers
    # only care about the hits, and we normalise the null case to an empty list
    # so the frontend doesn't have to handle null vs [] both ways.
    instances = instances_envelope.get("hits") or []
    if not isinstance(instances, list):
        instances = []

    # /instances/hits omits the per-instance printer compatibility info that
    # /design.instances[].extention.modelInfo carries (compatibility +
    # otherCompatibility). Merge it in so the frontend can show "this
    # instance was sliced for A1" + "also marked compatible with: H2D, P1S,
    # …" before the user picks one — without that, every instance row looks
    # identical in the UI and users blindly pick the first one regardless of
    # whether it matches their printer. (A.44 — per-instance MakerWorld
    # compat merge; also folds in A.43.4's per-instance compat surfacing.)
    design_instances = design.get("instances") or []
    if isinstance(design_instances, list):
        compat_by_id = {}
        for di in design_instances:
            if not isinstance(di, dict):
                continue
            iid = di.get("id")
            if iid is None:
                continue
            ext = (di.get("extention") or {}).get("modelInfo") or {}
            compat_by_id[iid] = {
                "compatibility": ext.get("compatibility"),
                "otherCompatibility": ext.get("otherCompatibility"),
            }
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            iid = inst.get("id")
            extra = compat_by_id.get(iid)
            if extra:
                inst["compatibility"] = extra["compatibility"]
                inst["otherCompatibility"] = extra["otherCompatibility"]

    # Find every library row whose source_url is either the model-level
    # canonical URL (legacy whole-model imports) or any plate-level URL
    # (``...#profileId-{n}``) under this model. The frontend surfaces this
    # to mark imported plates in the instance picker.
    model_prefix = _canonical_url(model_id)
    existing_q = await db.execute(
        select(
            LibraryFile.id,
            LibraryFile.source_url,
            LibraryFile.folder_id,
            LibraryFile.filename,
        ).where(
            (LibraryFile.source_url == model_prefix) | (LibraryFile.source_url.like(f"{model_prefix}#profileId-%")),
            LibraryFile.deleted_at.is_(None),
        )
    )
    already_imported_rows = list(existing_q.all())
    already_imported = [row[0] for row in already_imported_rows]

    # Build the per-variant dedupe map by re-parsing the source_url. The
    # ``#profileId-N`` fragment is appended by ``_canonical_url`` — pulling
    # it out here keeps the route as the only place that needs to know
    # the URL shape (the frontend can stay shape-agnostic).
    import re as _re

    profile_re = _re.compile(rf"^{_re.escape(model_prefix)}#profileId-(\d+)$")
    already_imported_by_profile: dict[str, MakerWorldAlreadyImportedEntry] = {}
    for lib_id, src, folder_id_val, filename_val in already_imported_rows:
        entry = MakerWorldAlreadyImportedEntry(
            library_file_id=lib_id,
            folder_id=folder_id_val,
            filename=filename_val,
        )
        if src == model_prefix:
            # Legacy whole-model import — keep under the conventional "0"
            # bucket so the frontend can surface a "this model was imported
            # before any plate was promoted" badge.
            already_imported_by_profile.setdefault("0", entry)
            continue
        m = profile_re.match(src or "")
        if m:
            # str keys: JSON doesn't allow int dict keys.
            already_imported_by_profile.setdefault(m.group(1), entry)

    return MakerWorldResolvedModel(
        model_id=model_id,
        profile_id=profile_id,
        design=design,
        instances=instances,
        already_imported_library_ids=already_imported,
        already_imported_by_profile_id=already_imported_by_profile,
    )


@router.post("/import", response_model=MakerWorldImportResponse)
async def import_instance(
    body: MakerWorldImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.MAKERWORLD_IMPORT),
):
    """Download a specific MakerWorld instance (plate configuration) and save
    the 3MF into the library.

    De-duplicates by canonicalised source URL — if the same MakerWorld plate
    was imported before, that existing LibraryFile is returned and no new
    download happens.
    """
    if body.folder_id is not None:
        # Eager-load .projects so save_3mf_bytes_to_library's
        # inherit_folder_projects() doesn't trip async lazy-load.
        folder_q = await db.execute(
            select(LibraryFolder)
            .where(LibraryFolder.id == body.folder_id)
            .options(selectinload(LibraryFolder.projects))
        )
        target_folder = folder_q.scalar_one_or_none()
        if target_folder is None:
            raise HTTPException(status_code=404, detail="Folder not found")
        if target_folder.is_external and target_folder.external_readonly:
            raise HTTPException(
                status_code=403,
                detail="Cannot import into a read-only external folder",
            )
        effective_folder: LibraryFolder | None = target_folder
    else:
        # Default destination: a dedicated top-level "MakerWorld" folder. Keeps
        # imports out of the library root so power users can still organise
        # manually in subfolders, and auto-creates the folder on the first
        # import so users don't have to set it up themselves.
        mw_folder_q = await db.execute(
            select(LibraryFolder).where(
                LibraryFolder.name == "MakerWorld",
                LibraryFolder.parent_id.is_(None),
                LibraryFolder.is_external.is_(False),
            )
        )
        mw_folder = mw_folder_q.scalar_one_or_none()
        if mw_folder is None:
            mw_folder = LibraryFolder(name="MakerWorld", parent_id=None)
            db.add(mw_folder)
            await db.flush()
        effective_folder = mw_folder

    service = await _build_service(db, current_user)

    # YASTL#51's iot-service endpoint needs the *alphanumeric* modelId
    # (e.g. "US2bb73b106683e5"), not the integer design id from /models/{N}.
    # Fetch design metadata to resolve it, and — in the same call — pick a
    # default profileId from the response if the frontend didn't specify one.
    try:
        design = await service.get_design(body.model_id)
    except MakerWorldError as exc:
        await service.close()
        raise _map_service_error(exc) from exc

    alphanumeric_model_id = design.get("modelId")
    if not isinstance(alphanumeric_model_id, str) or not alphanumeric_model_id:
        await service.close()
        raise HTTPException(
            status_code=502,
            detail="MakerWorld design metadata missing the modelId field",
        )

    profile_id = body.profile_id
    if profile_id is None:
        for instance in design.get("instances") or []:
            pid = instance.get("profileId")
            if isinstance(pid, int) and pid > 0:
                profile_id = pid
                break
        if profile_id is None:
            try:
                envelope = await service.get_design_instances(body.model_id)
            except MakerWorldError as exc:
                await service.close()
                raise _map_service_error(exc) from exc
            for hit in envelope.get("hits") or []:
                pid = hit.get("profileId")
                if isinstance(pid, int) and pid > 0:
                    profile_id = pid
                    break
        if profile_id is None:
            await service.close()
            raise HTTPException(
                status_code=502,
                detail="MakerWorld returned no instances for this model",
            )

    # Canonical URL includes profile_id so each plate gets its own library
    # entry (see ``_canonical_url`` docstring).
    source_url = _canonical_url(body.model_id, profile_id)

    try:
        manifest = await service.get_profile_download(profile_id, alphanumeric_model_id)
    except MakerWorldError as exc:
        await service.close()
        raise _map_service_error(exc) from exc

    signed_url = manifest.get("url")
    # Basename-strip any path components from the upstream filename so a
    # malicious response (``name: "../../evil.3mf"``) can't persist a suspect
    # string into the library row or the UI. On-disk storage uses a UUID
    # filename regardless (see library.py), so this is defence-in-depth.
    raw_name = manifest.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        # MakerWorld emits percent-encoded names (`%20` for spaces, etc.)
        # because the same string round-trips through HTTP URLs in the
        # CDN download path. Decode before persisting so the library
        # row, the slice toast, and every later UI surface show the
        # human-readable form.
        suggested_name = os.path.basename(unquote(raw_name.strip())) or f"makerworld-{body.model_id}.3mf"
    else:
        suggested_name = f"makerworld-{body.model_id}.3mf"
    if not signed_url or not isinstance(signed_url, str):
        await service.close()
        raise HTTPException(status_code=502, detail="MakerWorld did not return a download URL")

    # Dedupe check upfront so we don't burn bandwidth re-downloading.
    if source_url:
        existing_q = await db.execute(LibraryFile.active().where(LibraryFile.source_url == source_url).limit(1))
        existing_row = existing_q.scalar_one_or_none()
        if existing_row is not None:
            await service.close()
            return MakerWorldImportResponse(
                library_file_id=existing_row.id,
                filename=existing_row.filename,
                folder_id=existing_row.folder_id,
                profile_id=profile_id,
                was_existing=True,
            )

    try:
        file_bytes, download_filename = await service.download_3mf(signed_url)
    except MakerWorldError as exc:
        await service.close()
        raise _map_service_error(exc) from exc
    # NOTE: service stays open here — the post-import meta block below
    # reuses it for /instances + cover downloads, then closes it itself.

    # Prefer the server-provided human-readable filename; the signed URL's
    # path ends in a UUID that's not meaningful to users. Decode the
    # fallback path-tail too — same percent-encoding round-trip applies
    # there as on the manifest-supplied name.
    filename = suggested_name if suggested_name.endswith(".3mf") else unquote(download_filename)

    library_file, was_existing = await save_3mf_bytes_to_library(
        db,
        content=file_bytes,
        filename=filename,
        folder=effective_folder,
        created_by_id=current_user.id if current_user else None,
        source_type=_SOURCE_TYPE,
        source_url=source_url,
    )

    # Stash detailed MakerWorld metadata + download covers locally. Reuses
    # the same service instance so we don't make a third client. Wrapped
    # in a separate try so a meta failure never breaks the import — the
    # 3MF is already on disk and the LibraryFile row is committed.
    if not was_existing:
        try:
            envelope = await service.get_design_instances(body.model_id)
            instances = envelope.get("hits") if isinstance(envelope.get("hits"), list) else []
            variant_cover_url: str | None = None
            for inst in instances or []:
                if isinstance(inst, dict) and inst.get("profileId") == profile_id:
                    cov = inst.get("cover")
                    if isinstance(cov, str):
                        variant_cover_url = cov
                    break
            meta_dict = build_meta_dict(
                library_file_id=library_file.id,
                design=design,
                instances=instances or [],
                profile_id=profile_id,
                variant_url=source_url,
                model_id_alphanumeric=alphanumeric_model_id,
            )
            cover_url = design.get("coverUrl") if isinstance(design.get("coverUrl"), str) else None
            cover_rel, variant_cover_rel = await download_covers(
                service,
                library_file_id=library_file.id,
                cover_url=cover_url,
                variant_cover_url=variant_cover_url,
            )
            db.add(
                LibraryFileMakerworldMeta(
                    **meta_dict,
                    cover_path=cover_rel,
                    variant_cover_path=variant_cover_rel,
                )
            )
            await db.commit()
        except Exception as exc:
            logger.warning("MakerWorld meta save failed for library_file_id=%s: %s", library_file.id, exc)
            await db.rollback()
        finally:
            # close the service before returning — original code closed
            # it before save_3mf_bytes_to_library, but we now need it for
            # cover downloads, so the close moves here.
            await service.close()
    else:
        await service.close()

    return MakerWorldImportResponse(
        library_file_id=library_file.id,
        filename=library_file.filename,
        folder_id=library_file.folder_id,
        profile_id=profile_id,
        was_existing=was_existing,
    )


def _row_to_recent_import(row: LibraryFile, meta: LibraryFileMakerworldMeta | None) -> MakerWorldRecentImport:
    """Project a (library_file, meta?) pair into the wire shape."""
    return MakerWorldRecentImport(
        library_file_id=row.id,
        filename=row.filename,
        folder_id=row.folder_id,
        thumbnail_path=row.thumbnail_path,
        source_url=row.source_url,
        created_at=row.created_at.isoformat() if row.created_at else "",
        title=meta.title if meta else None,
        author_name=meta.author_name if meta else None,
        sliced_for=meta.sliced_for if meta else None,
        profile_id=meta.profile_id if meta else None,
        has_cover=bool(meta and meta.cover_path),
        has_variant_cover=bool(meta and meta.variant_cover_path),
    )


@router.post(
    "/imports/{library_file_id}/redownload",
    response_model=MakerWorldImportResponse,
)
async def redownload_import(
    library_file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.MAKERWORLD_IMPORT),
):
    """Re-download a previously imported MakerWorld variant — overwrites
    on-disk bytes, refreshes the meta row, re-downloads covers.

    Keeps the same ``library_file_id`` (and FK references — queue items,
    project links, archives) intact: only the file bytes + metadata are
    refreshed. The dedupe-skip on ``/import`` deliberately means clicking
    Import again is a no-op; users who actually want fresh bytes (the
    creator pushed an update on MakerWorld) come through this endpoint.
    """
    import re as _re
    from pathlib import Path

    from backend.app.api.routes.library import calculate_file_hash, to_absolute_path
    from backend.app.services.archive import ThreeMFParser
    from backend.app.services.makerworld_meta import build_meta_dict, download_covers

    lib = (await db.execute(LibraryFile.active().where(LibraryFile.id == library_file_id))).scalar_one_or_none()
    if lib is None:
        raise HTTPException(status_code=404, detail="Library file not found")
    if lib.source_type != _SOURCE_TYPE or not lib.source_url:
        raise HTTPException(
            status_code=400,
            detail="This library file is not a MakerWorld import",
        )

    # Parse model_id + (optional) profile_id back out of the canonical URL.
    # _canonical_url is the only producer of this shape, so the regex is
    # stable.
    match = _re.match(
        r"^https://makerworld\.com/models/(\d+)(?:#profileId-(\d+))?$",
        lib.source_url,
    )
    if not match:
        raise HTTPException(
            status_code=400,
            detail=f"Unparseable MakerWorld source_url: {lib.source_url!r}",
        )
    model_id = int(match.group(1))
    profile_id = int(match.group(2)) if match.group(2) else None

    service = await _build_service(db, current_user)
    try:
        try:
            design = await service.get_design(model_id)
        except MakerWorldError as exc:
            raise _map_service_error(exc) from exc

        alphanumeric_model_id = design.get("modelId")
        if not isinstance(alphanumeric_model_id, str) or not alphanumeric_model_id:
            raise HTTPException(
                status_code=502,
                detail="MakerWorld design metadata missing the modelId field",
            )

        if profile_id is None:
            # Legacy whole-model rows have no profile_id in the URL. Pick
            # the first available variant (same fallback the import-path
            # uses) so we still get fresh bytes.
            for instance in design.get("instances") or []:
                pid = instance.get("profileId")
                if isinstance(pid, int) and pid > 0:
                    profile_id = pid
                    break
            if profile_id is None:
                raise HTTPException(
                    status_code=502,
                    detail="MakerWorld returned no instances for this model",
                )

        try:
            manifest = await service.get_profile_download(profile_id, alphanumeric_model_id)
        except MakerWorldError as exc:
            raise _map_service_error(exc) from exc

        signed_url = manifest.get("url")
        if not signed_url or not isinstance(signed_url, str):
            raise HTTPException(status_code=502, detail="MakerWorld did not return a download URL")

        try:
            file_bytes, _download_filename = await service.download_3mf(signed_url)
        except MakerWorldError as exc:
            raise _map_service_error(exc) from exc

        # Overwrite the existing file on disk. We deliberately keep
        # ``lib.file_path`` + ``lib.filename`` unchanged so any external
        # references (queue items pointing at the on-disk path, project
        # BOM links, etc.) stay valid.
        abs_path = to_absolute_path(lib.file_path)
        if abs_path is None:
            raise HTTPException(status_code=500, detail="Library file has no resolvable on-disk path")
        with open(abs_path, "wb") as fh:
            fh.write(file_bytes)

        # Refresh hash / size / metadata. Mirrors the on-import branch
        # in ``save_3mf_bytes_to_library`` but operates on the existing row.
        lib.file_size = len(file_bytes)
        lib.file_hash = calculate_file_hash(Path(abs_path))
        try:
            parser = ThreeMFParser(str(abs_path))
            raw_metadata = parser.parse()
            from backend.app.api.routes.library import _clean_3mf_metadata

            lib.file_metadata = _clean_3mf_metadata(raw_metadata)
        except Exception:  # noqa: BLE001
            logger.debug("redownload: 3MF re-parse failed (non-critical)")

        # Refresh meta row + covers. We reuse the open ``service`` for
        # the /instances call + cover downloads.
        try:
            envelope = await service.get_design_instances(model_id)
            instances = envelope.get("hits") if isinstance(envelope.get("hits"), list) else []
            variant_cover_url: str | None = None
            for inst in instances or []:
                if isinstance(inst, dict) and inst.get("profileId") == profile_id:
                    cov = inst.get("cover")
                    if isinstance(cov, str):
                        variant_cover_url = cov
                    break
            meta_dict = build_meta_dict(
                library_file_id=lib.id,
                design=design,
                instances=instances or [],
                profile_id=profile_id,
                variant_url=lib.source_url,
                model_id_alphanumeric=alphanumeric_model_id,
            )
            cover_url = design.get("coverUrl") if isinstance(design.get("coverUrl"), str) else None
            cover_rel, variant_cover_rel = await download_covers(
                service,
                library_file_id=lib.id,
                cover_url=cover_url,
                variant_cover_url=variant_cover_url,
            )

            existing_meta = (
                await db.execute(
                    select(LibraryFileMakerworldMeta).where(LibraryFileMakerworldMeta.library_file_id == lib.id)
                )
            ).scalar_one_or_none()
            if existing_meta is None:
                db.add(
                    LibraryFileMakerworldMeta(
                        **meta_dict,
                        cover_path=cover_rel,
                        variant_cover_path=variant_cover_rel,
                    )
                )
            else:
                for k, v in meta_dict.items():
                    if k == "library_file_id":
                        continue
                    setattr(existing_meta, k, v)
                existing_meta.cover_path = cover_rel
                existing_meta.variant_cover_path = variant_cover_rel
        except Exception as exc:  # noqa: BLE001
            logger.warning("redownload: meta refresh failed for library_file_id=%s: %s", lib.id, exc)

        await db.commit()
        await db.refresh(lib)
    finally:
        await service.close()

    return MakerWorldImportResponse(
        library_file_id=lib.id,
        filename=lib.filename,
        folder_id=lib.folder_id,
        profile_id=profile_id,
        was_existing=True,
    )


@router.get("/recent-imports", response_model=list[MakerWorldRecentImport])
async def recent_imports(
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.MAKERWORLD_VIEW),
):
    """Last N MakerWorld imports, newest first.

    Compact summary for "recent" widgets; the History tab uses the
    paginated :func:`list_imports` endpoint below instead. ``limit`` is
    clamped to ``[1, 50]`` to keep payloads sensible.
    """
    _ = current_user  # permission gate only
    capped = max(1, min(50, int(limit)))
    result = await db.execute(
        LibraryFile.active()
        .where(LibraryFile.source_type == _SOURCE_TYPE)
        .order_by(LibraryFile.created_at.desc())
        .limit(capped)
    )
    rows = list(result.scalars().all())
    if not rows:
        return []
    meta_by_id: dict[int, LibraryFileMakerworldMeta] = {}
    meta_rows = await db.execute(
        select(LibraryFileMakerworldMeta).where(LibraryFileMakerworldMeta.library_file_id.in_([r.id for r in rows]))
    )
    for meta in meta_rows.scalars().all():
        meta_by_id[meta.library_file_id] = meta
    return [_row_to_recent_import(row, meta_by_id.get(row.id)) for row in rows]


@router.get("/imports", response_model=MakerWorldImportsPage)
async def list_imports(
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=200),
    search: str | None = Query(None, description="Match against filename / meta title / author"),
    sort_by: str = Query("date-desc", description="One of date-desc / date-asc / name-asc / name-desc"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.MAKERWORLD_VIEW),
):
    """Paginated, searchable, sortable MakerWorld import history.

    Drives the "Історія" tab grid on the MakerWorld page. Joins
    ``library_files`` with ``library_file_makerworld_meta`` so search can
    match meta fields (title, author) on top of the raw filename. Sort
    options mirror the Archives page.
    """
    from sqlalchemy import or_

    base_filters = [
        LibraryFile.source_type == _SOURCE_TYPE,
        LibraryFile.deleted_at.is_(None),
    ]

    if search:
        like = f"%{search.strip()}%"
        base_filters.append(
            or_(
                LibraryFile.filename.ilike(like),
                LibraryFileMakerworldMeta.title.ilike(like),
                LibraryFileMakerworldMeta.author_name.ilike(like),
            )
        )

    # We always LEFT JOIN meta so a search on meta fields can hit it; rows
    # without meta still surface (their meta columns are NULL → won't
    # match the OR-clause, which is correct).
    query_base = (
        select(LibraryFile)
        .outerjoin(
            LibraryFileMakerworldMeta,
            LibraryFileMakerworldMeta.library_file_id == LibraryFile.id,
        )
        .where(*base_filters)
    )

    # Total count for pagination meta — same WHERE/JOIN, no ORDER/LIMIT.
    from sqlalchemy import func as _func

    count_q = (
        select(_func.count())
        .select_from(
            LibraryFile.__table__.outerjoin(
                LibraryFileMakerworldMeta.__table__,
                LibraryFileMakerworldMeta.library_file_id == LibraryFile.id,
            )
        )
        .where(*base_filters)
    )
    total = (await db.execute(count_q)).scalar() or 0

    sort_map = {
        "date-desc": LibraryFile.created_at.desc(),
        "date-asc": LibraryFile.created_at.asc(),
        "name-asc": LibraryFile.filename.asc(),
        "name-desc": LibraryFile.filename.desc(),
    }
    order_clause = sort_map.get(sort_by, sort_map["date-desc"])

    offset = (page - 1) * per_page
    rows_q = query_base.order_by(order_clause).limit(per_page).offset(offset)
    rows = list((await db.execute(rows_q)).scalars().all())

    meta_by_id: dict[int, LibraryFileMakerworldMeta] = {}
    if rows:
        meta_rows = await db.execute(
            select(LibraryFileMakerworldMeta).where(LibraryFileMakerworldMeta.library_file_id.in_([r.id for r in rows]))
        )
        for meta in meta_rows.scalars().all():
            meta_by_id[meta.library_file_id] = meta

    import math as _math

    last_page = max(1, _math.ceil(total / per_page)) if total else 1

    return MakerWorldImportsPage(
        data=[_row_to_recent_import(row, meta_by_id.get(row.id)) for row in rows],
        meta=MakerWorldImportsPaginationMeta(
            total=total,
            current_page=page,
            per_page=per_page,
            last_page=last_page,
        ),
    )


@router.get(
    "/imports/{library_file_id}/meta",
    response_model=LibraryFileMakerworldMetaResponse,
)
async def get_makerworld_meta(
    library_file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.MAKERWORLD_VIEW),
):
    """Get the MakerWorld metadata row for a library file."""
    meta = (
        await db.execute(
            select(LibraryFileMakerworldMeta).where(LibraryFileMakerworldMeta.library_file_id == library_file_id)
        )
    ).scalar_one_or_none()
    if meta is None:
        raise HTTPException(status_code=404, detail="No MakerWorld metadata for this library file")
    return LibraryFileMakerworldMetaResponse(
        library_file_id=meta.library_file_id,
        title=meta.title,
        description=meta.description,
        author_name=meta.author_name,
        author_profile_url=meta.author_profile_url,
        license=meta.license,
        original_design_id=meta.original_design_id,
        variant_title=meta.variant_title,
        variant_description=meta.variant_description,
        variant_url=meta.variant_url,
        profile_id=meta.profile_id,
        sliced_for=meta.sliced_for,
        compatible_models=meta.compatible_models,
        needs_ams=meta.needs_ams,
        material_count=meta.material_count,
        materials=meta.materials,
        model_id_alphanumeric=meta.model_id_alphanumeric,
        has_cover=bool(meta.cover_path),
        has_variant_cover=bool(meta.variant_cover_path),
        imported_at=meta.imported_at,
    )


def _serve_local_cover(rel_path: str | None) -> Response:
    """Serve a cover image file from disk with proper Content-Type."""
    if not rel_path:
        raise HTTPException(status_code=404, detail="Cover not available")
    from pathlib import Path as _Path

    from backend.app.core.config import settings as _settings

    abs_path = _Path(_settings.base_dir) / rel_path
    if not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(status_code=404, detail="Cover file missing")
    ext = abs_path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
    return Response(content=abs_path.read_bytes(), media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/imports/{library_file_id}/cover")
async def get_makerworld_cover(
    library_file_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Serve the model-level cover image saved locally during import.

    No ``RequirePermission`` here — ``<img src>`` browser fetches can't
    carry an Authorization header. The auth-middleware whitelist treats
    URL paths containing ``/cover`` as public (same pattern used by
    library file thumbnails / printer covers). The data exposed is the
    same image MakerWorld serves publicly on their site, so this isn't a
    privacy regression.
    """
    meta = (
        await db.execute(
            select(LibraryFileMakerworldMeta.cover_path).where(
                LibraryFileMakerworldMeta.library_file_id == library_file_id
            )
        )
    ).scalar_one_or_none()
    return _serve_local_cover(meta)


@router.get("/imports/{library_file_id}/cover-variant")
async def get_makerworld_variant_cover(
    library_file_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Serve the variant (plate-level) cover image saved locally.

    Path intentionally ends with ``cover-variant`` (not ``variant-cover``)
    so the ``/cover`` substring still matches the auth-middleware public
    whitelist — same reasoning as :func:`get_makerworld_cover`.
    """
    meta = (
        await db.execute(
            select(LibraryFileMakerworldMeta.variant_cover_path).where(
                LibraryFileMakerworldMeta.library_file_id == library_file_id
            )
        )
    ).scalar_one_or_none()
    return _serve_local_cover(meta)
