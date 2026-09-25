"""MakerWorld integration routes.

User pastes a MakerWorld URL → BamDude resolves it → shows plate list →
one-click import/print. The URL-paste flow covers the actual discovery
pattern (Reddit/YouTube/shared links) without needing to replicate
MakerWorld's whole search UI.

Search/browse endpoints are intentionally NOT exposed: the public-facing
``design/search`` endpoint returns empty results from server-originated
requests.

The routes reach the site only through the model-provider seam
(``services/model_providers``, upstream #2845): the registry picks the
provider for a pasted URL or a named ``source_type``, the descriptor owns the
URL shape, the folder name and the permissions, and a per-request
``ProviderService`` does the network work. The endpoints, their shapes and
the import history / meta / covers stay MakerWorld's — a second site gets
its own storage and UI when it arrives.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.cloud import resolve_api_key_cloud_owner
from backend.app.api.routes.library import save_3mf_bytes_to_library
from backend.app.core.auth import RequirePermission, require_permission, security
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
from backend.app.services.model_providers import makerworld_provider, registry
from backend.app.services.model_providers.base import (
    ModelProvider,
    ProviderAuthError,
    ProviderError,
    ProviderForbiddenError,
    ProviderNotFoundError,
    ProviderResourceRef,
    ProviderService,
    ProviderUnavailableError,
    ProviderUrlError,
)
from backend.app.services.model_providers.makerworld.meta import build_meta_dict, download_covers
from backend.app.services.model_providers.makerworld.service import MakerWorldService
from backend.app.services.product_sync import resync_file_products

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/makerworld", tags=["makerworld"])

_SOURCE_TYPE = makerworld_provider.source_type


def _provider_for_url(url: str) -> ModelProvider:
    """The registered provider that claims *url* — a pasted link for a host
    nobody serves is a client-input problem, a 400."""
    provider = registry.find_for_url(url)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"This link is not from a supported model site: {url!r}")
    return provider


def _provider_for_source(source_type: str) -> ModelProvider:
    """The registered provider with this ``source_type``.

    Import names a resource by id, not by URL, so the source type is all
    there is to route on. The detail is built here: ``str(KeyError)`` is the
    repr of its argument and would ship the quotes to the client.
    """
    try:
        return registry.get(source_type)
    except KeyError as exc:
        raise HTTPException(
            status_code=400, detail=f"No model provider registered for source_type {source_type!r}"
        ) from exc


async def _authorize_for_provider(
    provider: ModelProvider,
    permission: Permission | None,
    route_permission: Permission,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    x_api_key: str | None,
) -> None:
    """Apply *provider*'s own permission on top of the route's.

    The route already checked ``route_permission`` (MakerWorld's — every
    endpoint names its permission). Which provider a request uses is known
    only once the body is read (``source_type`` on import, the pasted URL on
    resolve), so a provider whose permission differs is checked here: a
    second model site is never imported under MakerWorld's permission
    (upstream #2845). A provider that declares none is refused rather than
    read as unrestricted.
    """
    if permission is None:
        raise HTTPException(
            status_code=500,
            detail=f"Model provider {provider.source_type!r} declares no permission for this operation",
        )
    if permission == route_permission:
        return
    await require_permission(permission)(request=request, credentials=credentials, x_api_key=x_api_key)


async def _build_service(
    db: AsyncSession,
    provider: ModelProvider,
    current_user: User | None,
    api_key_cloud_owner: User | None = None,
) -> ProviderService:
    """One per-request service, built by *provider*.

    Identity (JWT user, else the API key's owner, else the ownerless global
    token) and credential seeding live in ``provider.build_service`` — the
    routes never re-implement them.
    """
    return await provider.build_service(db=db, user=current_user, api_key_owner=api_key_cloud_owner)


def _map_service_error(exc: ProviderError) -> HTTPException:
    """Translate provider service exceptions into HTTP responses."""
    if isinstance(exc, ProviderUrlError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ProviderAuthError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, ProviderForbiddenError):
        # 403 forwards the site's own refusal message (content-gated,
        # region-locked, requires points, etc.) — UI surfaces it verbatim.
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ProviderNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ProviderUnavailableError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=f"Model provider error: {exc}")


async def _fetch_makerworld_meta(
    service: MakerWorldService,
    *,
    library_file_id: int,
    model_id: int,
    profile_id: int | None,
    variant_url: str,
) -> dict[str, Any]:
    """Everything a ``LibraryFileMakerworldMeta`` row holds for one plate —
    the design (already fetched by ``get_download``; memoised in the
    service), the instance list, and the two covers written to disk."""
    design = await service.get_design(model_id)
    envelope = await service.get_design_instances(model_id)
    instances = envelope.get("hits") if isinstance(envelope.get("hits"), list) else []
    variant_cover_url: str | None = None
    for inst in instances or []:
        if isinstance(inst, dict) and inst.get("profileId") == profile_id:
            cov = inst.get("cover")
            if isinstance(cov, str):
                variant_cover_url = cov
            break
    alphanumeric_model_id = design.get("modelId") if isinstance(design.get("modelId"), str) else None
    meta_dict = build_meta_dict(
        library_file_id=library_file_id,
        design=design,
        instances=instances or [],
        profile_id=profile_id,
        variant_url=variant_url,
        model_id_alphanumeric=alphanumeric_model_id,
    )
    cover_url = design.get("coverUrl") if isinstance(design.get("coverUrl"), str) else None
    cover_rel, variant_cover_rel = await download_covers(
        service,
        library_file_id=library_file_id,
        cover_url=cover_url,
        variant_cover_url=variant_cover_url,
    )
    return {**meta_dict, "cover_path": cover_rel, "variant_cover_path": variant_cover_rel}


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
    the upstream host to the provider's declared CDN allowlist, so this can't
    be abused as a generic open proxy. Whitelisted in ``auth_middleware`` so
    the always-on auth gate doesn't 401 the proxied image fetch.

    URLs are content-addressable (filename contains a hash), so the
    aggressive ``immutable`` cache-control is safe.
    """
    service = MakerWorldService(thumbnail_hosts=makerworld_provider.thumbnail_hosts())
    try:
        payload, content_type = await service.fetch_thumbnail(url)
    except ProviderError as exc:
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
    current_user: User | None = RequirePermission(makerworld_provider.view_permission),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
):
    """Report whether the caller can import 3MFs (needs a Bambu Cloud token).

    API-keyed callers (which return None from ``current_user``) get the owner
    User via ``resolve_api_key_cloud_owner`` when the key carries the
    cloud-access scope, so ``has_cloud_token`` reflects the owning user's
    stored token rather than always reporting ``False`` (#1777).

    ``sign_in_expired`` is the provider's ``credential_rejected``: a stored
    token Bambu has refused. It is its own state — "sign in" said to someone
    who believes they already are was the confusion #2562 fixed.
    """
    service = await _build_service(db, makerworld_provider, current_user, api_key_cloud_owner)
    try:
        status = await service.get_status(db)
    finally:
        await service.close()
    return MakerWorldStatus(
        has_cloud_token=status.authenticated,
        can_download=status.can_download,
        sign_in_expired=status.credential_rejected,
    )


@router.post("/resolve", response_model=MakerWorldResolvedModel)
async def resolve_url(
    body: MakerWorldResolveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(makerworld_provider.view_permission),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Resolve a MakerWorld URL to full model metadata + plate list.

    The response also tells the caller which (if any) LibraryFile rows already
    exist for the same model URL, so the UI can show an "Already imported"
    badge and skip a redundant download.
    """
    provider = _provider_for_url(body.url)
    await _authorize_for_provider(
        provider, provider.view_permission, makerworld_provider.view_permission, request, credentials, x_api_key
    )
    try:
        ref = provider.parse_url(body.url)
    except ProviderError as exc:
        raise _map_service_error(exc) from exc
    model_id = int(ref.external_id)
    profile_id = int(ref.sub_id) if ref.sub_id else None

    service = await _build_service(db, provider, current_user, api_key_cloud_owner)
    try:
        # The provider merges MakerWorld's per-instance printer compatibility
        # into the instance list (A.44) — the route passes it through.
        resolved = await service.resolve(ref)
    except ProviderError as exc:
        raise _map_service_error(exc) from exc
    finally:
        await service.close()

    # Every library row that belongs to this model — the provider's
    # ``source_url_filter`` owns what "belongs" means (the whole-model key
    # plus every ``#profileId-{n}`` plate key for MakerWorld).
    existing_q = await db.execute(
        select(
            LibraryFile.id,
            LibraryFile.source_url,
            LibraryFile.folder_id,
            LibraryFile.filename,
        ).where(
            provider.source_url_filter(LibraryFile.source_url, str(model_id)),
            LibraryFile.deleted_at.is_(None),
        )
    )
    already_imported_rows = list(existing_q.all())
    already_imported = [row[0] for row in already_imported_rows]

    # Per-plate dedupe map for the instance picker, keyed by the plate's
    # profile id as a string (JSON has no int keys). Each row's key comes back
    # through the provider's own URL parser.
    model_key = provider.canonical_url(ProviderResourceRef(source_type=provider.source_type, external_id=str(model_id)))
    already_imported_by_profile: dict[str, MakerWorldAlreadyImportedEntry] = {}
    for lib_id, src, folder_id_val, filename_val in already_imported_rows:
        entry = MakerWorldAlreadyImportedEntry(
            library_file_id=lib_id,
            folder_id=folder_id_val,
            filename=filename_val,
        )
        if src == model_key:
            # Legacy whole-model import — keep under the conventional "0"
            # bucket so the frontend can surface a "this model was imported
            # before any plate was promoted" badge.
            already_imported_by_profile.setdefault("0", entry)
            continue
        try:
            row_ref = provider.parse_url(src or "")
        except ProviderError:
            continue
        if row_ref.sub_id:
            already_imported_by_profile.setdefault(row_ref.sub_id, entry)

    return MakerWorldResolvedModel(
        model_id=model_id,
        profile_id=profile_id,
        design=resolved.design,
        instances=resolved.instances,
        already_imported_library_ids=already_imported,
        already_imported_by_profile_id=already_imported_by_profile,
    )


@router.post("/import", response_model=MakerWorldImportResponse)
async def import_instance(
    body: MakerWorldImportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(makerworld_provider.import_permission),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Download a specific MakerWorld instance (plate configuration) and save
    the 3MF into the library.

    De-duplicates by canonicalised source URL — if the same MakerWorld plate
    was imported before, that existing LibraryFile is returned and no new
    download happens.
    """
    # The provider first: an unknown ``source_type`` is a 400 before the
    # default folder gets auto-created as a side effect, and the permission
    # that applies on top of the route's is the resolved provider's.
    provider = _provider_for_source(body.source_type)
    await _authorize_for_provider(
        provider, provider.import_permission, makerworld_provider.import_permission, request, credentials, x_api_key
    )

    effective_folder: LibraryFolder | None
    if body.folder_id is not None:
        # Eager-load .products so save_3mf_bytes_to_library's
        # inherit_folder_products() doesn't trip async lazy-load.
        folder_q = await db.execute(
            select(LibraryFolder)
            .where(LibraryFolder.id == body.folder_id)
            .options(selectinload(LibraryFolder.products))
        )
        target_folder = folder_q.scalar_one_or_none()
        if target_folder is None:
            raise HTTPException(status_code=404, detail="Folder not found")
        if target_folder.is_external and target_folder.external_readonly:
            raise HTTPException(
                status_code=403,
                detail="Cannot import into a read-only external folder",
            )
        effective_folder = target_folder
    elif provider.default_folder_name is None:
        # A provider without a default folder imports into the library root
        # rather than minting a NULL-named folder.
        effective_folder = None
    else:
        # Default destination: the provider's dedicated top-level folder
        # ("MakerWorld"). Keeps imports out of the library root so power users
        # can still organise manually in subfolders, and auto-creates the
        # folder on the first import so users don't have to set it up.
        default_folder_q = await db.execute(
            select(LibraryFolder).where(
                LibraryFolder.name == provider.default_folder_name,
                LibraryFolder.parent_id.is_(None),
                LibraryFolder.is_external.is_(False),
            )
        )
        default_folder = default_folder_q.scalar_one_or_none()
        if default_folder is None:
            default_folder = LibraryFolder(name=provider.default_folder_name, parent_id=None)
            db.add(default_folder)
            await db.flush()
        effective_folder = default_folder

    # API-keyed callers carry identity on the key, not in current_user — see
    # the /status handler comment and #1777. The same resolved user is reused
    # for created_by_id on save_3mf_bytes_to_library below so the library row
    # is attributed to the key's owner rather than NULL.
    cloud_token_user = current_user or api_key_cloud_owner
    service = await _build_service(db, provider, current_user, api_key_cloud_owner)
    # One close for the whole request: the meta row and the covers below reuse
    # this service after the 3MF is saved.
    try:
        ref = ProviderResourceRef(
            source_type=provider.source_type,
            external_id=str(body.model_id),
            sub_id=str(body.profile_id) if body.profile_id else None,
        )
        # MakerWorld's iot-service needs the alphanumeric modelId and a plate;
        # ``get_download`` resolves both (the first plate when none was named)
        # and hands the chosen one back in ``info.ref``.
        try:
            info = await service.get_download(ref)
        except ProviderError as exc:
            raise _map_service_error(exc) from exc
        profile_id = int(info.ref.sub_id) if info.ref.sub_id else None
        # Canonical URL includes the plate so each plate gets its own library
        # entry (see ``ModelProvider.canonical_url``).
        source_url = provider.canonical_url(info.ref)

        # Dedupe check upfront so we don't burn bandwidth re-downloading.
        existing_q = await db.execute(LibraryFile.active().where(LibraryFile.source_url == source_url).limit(1))
        existing_row = existing_q.scalar_one_or_none()
        if existing_row is not None:
            return MakerWorldImportResponse(
                library_file_id=existing_row.id,
                filename=existing_row.filename,
                folder_id=existing_row.folder_id,
                profile_id=profile_id,
                was_existing=True,
            )

        try:
            download = await service.download(info)
        except ProviderError as exc:
            raise _map_service_error(exc) from exc

        # Basename-strip any path components from the upstream filename so a
        # malicious response (``name: "../../evil.3mf"``) can't persist a
        # suspect string into the library row or the UI. On-disk storage uses
        # a UUID filename regardless (see library.py), so this is
        # defence-in-depth. MakerWorld emits percent-encoded names (`%20` for
        # spaces, etc.) because the same string round-trips through HTTP URLs
        # in the CDN download path — decode before persisting so every UI
        # surface shows the human-readable form.
        raw_name = info.suggested_filename
        if isinstance(raw_name, str) and raw_name.strip():
            suggested_name = os.path.basename(unquote(raw_name.strip())) or f"makerworld-{body.model_id}.3mf"
        else:
            suggested_name = f"makerworld-{body.model_id}.3mf"
        # Prefer the server-provided human-readable filename; the signed URL's
        # path ends in a UUID that's not meaningful to users.
        filename = suggested_name if suggested_name.endswith(".3mf") else unquote(download.filename)

        result = await save_3mf_bytes_to_library(
            db,
            content=download.file_bytes,
            filename=filename,
            folder=effective_folder,
            created_by_id=cloud_token_user.id if cloud_token_user else None,
            source_type=provider.source_type,
            source_url=source_url,
        )
        # ⚠️ ``result.file`` may be a row this import did not create — either
        # the same source_url was imported before, or another row already
        # holds these exact bytes. Both mean the metadata work below has
        # already been done.
        library_file = result.file
        was_existing = result.outcome != "created"

        # MakerWorld's meta row + locally stored covers (m056 — MakerWorld's
        # own table). A meta failure never breaks the import: the 3MF is
        # already on disk and the LibraryFile row is committed.
        if not was_existing and provider is makerworld_provider:
            try:
                meta = await _fetch_makerworld_meta(
                    service,
                    library_file_id=library_file.id,
                    model_id=body.model_id,
                    profile_id=profile_id,
                    variant_url=source_url,
                )
                db.add(LibraryFileMakerworldMeta(**meta))
                await db.commit()
            except Exception as exc:
                logger.warning("MakerWorld meta save failed for library_file_id=%s: %s", library_file.id, exc)
                await db.rollback()
    finally:
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
    current_user: User | None = RequirePermission(makerworld_provider.import_permission),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
):
    """Re-download a previously imported MakerWorld variant — overwrites
    on-disk bytes, refreshes the meta row, re-downloads covers.

    Keeps the same ``library_file_id`` (and FK references — queue items,
    project links, archives) intact: only the file bytes + metadata are
    refreshed. The dedupe-skip on ``/import`` deliberately means clicking
    Import again is a no-op; users who actually want fresh bytes (the
    creator pushed an update on MakerWorld) come through this endpoint.
    """
    from pathlib import Path

    from backend.app.api.routes.library import calculate_file_hash, to_absolute_path
    from backend.app.services.archive import ThreeMFParser

    lib = (await db.execute(LibraryFile.active().where(LibraryFile.id == library_file_id))).scalar_one_or_none()
    if lib is None:
        raise HTTPException(status_code=404, detail="Library file not found")
    if lib.source_type != _SOURCE_TYPE or not lib.source_url:
        raise HTTPException(
            status_code=400,
            detail="This library file is not a MakerWorld import",
        )

    # The row's ``source_url`` is the provider's canonical key; its own parser
    # reads the model and (for a per-plate row) the plate back out. A legacy
    # whole-model row has no plate — ``get_download`` takes the design's first.
    try:
        ref = makerworld_provider.parse_url(lib.source_url)
    except ProviderError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unparseable MakerWorld source_url: {lib.source_url!r}",
        ) from exc
    model_id = int(ref.external_id)

    service = await _build_service(db, makerworld_provider, current_user, api_key_cloud_owner)
    try:
        try:
            info = await service.get_download(ref)
            download = await service.download(info)
        except ProviderError as exc:
            raise _map_service_error(exc) from exc
        profile_id = int(info.ref.sub_id) if info.ref.sub_id else None
        file_bytes = download.file_bytes

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

        # The plate set is derived from ``file_metadata``, which we have just
        # rewritten: re-run the sync against the file's CURRENT product links so
        # a re-download that added or dropped a plate is reflected in every
        # product this file belongs to. Flushed first — the sync reads the row
        # back, and it must read the new metadata, not the old.
        await db.flush()
        await resync_file_products(db, lib.id)

        # Refresh meta row + covers through the same open service.
        try:
            meta = await _fetch_makerworld_meta(
                service,
                library_file_id=lib.id,
                model_id=model_id,
                profile_id=profile_id,
                variant_url=lib.source_url,
            )
            existing_meta = (
                await db.execute(
                    select(LibraryFileMakerworldMeta).where(LibraryFileMakerworldMeta.library_file_id == lib.id)
                )
            ).scalar_one_or_none()
            if existing_meta is None:
                db.add(LibraryFileMakerworldMeta(**meta))
            else:
                for k, v in meta.items():
                    if k == "library_file_id":
                        continue
                    setattr(existing_meta, k, v)
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
    current_user: User | None = RequirePermission(makerworld_provider.view_permission),
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
    _: User | None = RequirePermission(makerworld_provider.view_permission),
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
    _: User | None = RequirePermission(makerworld_provider.view_permission),
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
    carry an Authorization header. ``main.py::PUBLIC_API_PATTERNS`` carries
    one anchored entry for this exact route
    (``^/api/v1/makerworld/imports/\\d+/cover$``), tagged ``anonymous`` in
    ``backend/tests/test_auth_public_patterns.py``: the data served is the
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

    Anchored in the whitelist under its own entry
    (``^/api/v1/makerworld/imports/\\d+/cover-variant$``) — same reasoning as
    :func:`get_makerworld_cover`. The path's spelling no longer matters to the
    gate: it once had to END in ``cover`` to satisfy a bare ``"/cover"``
    substring, which is exactly the matching that was removed.
    """
    meta = (
        await db.execute(
            select(LibraryFileMakerworldMeta.variant_cover_path).where(
                LibraryFileMakerworldMeta.library_file_id == library_file_id
            )
        )
    ).scalar_one_or_none()
    return _serve_local_cover(meta)
