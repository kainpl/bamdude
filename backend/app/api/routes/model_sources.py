"""Model sources with a catalogue: ``/model-sources/{source}/…``.

:func:`make_source_router` builds the SAME five routes for any registered
provider whose service can be browsed (``BrowsableService``): status, the
model list / search, one model with its files, a model's preview, and the
import of one file. Each route carries that provider's OWN permission as a
static ``RequirePermission`` — a user who holds only ``manyfold:*`` uses
Manyfold, and nothing here asks for another source's right.

MakerWorld is a paste-a-link source and stays on ``/makerworld/*``; both call
the one import core (``services/model_import``).

A source's refusals are answered as ``{"error": code, "message": …}`` with 400 /
404 / 409 / 413 / 502 — never 401 or 403, which are words about OUR session.

Below it, the editor of the installation's one Manyfold connection
(``settings:read`` / ``settings:update``): the client secret is write-only and
belongs to its destination.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission, require_media_permission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.app.services import model_import
from backend.app.services.library_ingest import read_access
from backend.app.services.model_providers import manyfold_provider
from backend.app.services.model_providers.base import (
    BrowsableService,
    ModelProvider,
    ProviderError,
    ProviderNotFoundError,
    ProviderResourceRef,
    ProviderService,
    ProviderUrlError,
)
from backend.app.services.model_providers.manyfold import config as manyfold_config, service as manyfold_service
from backend.app.utils.filename import InvalidFilenameError, validate_print_filename

logger = logging.getLogger(__name__)


class SourceStatus(BaseModel):
    source: str
    name: str
    configured: bool
    usable: bool
    #: The source's own address, for "open there" links. None when it has none to show.
    base_url: str | None = None
    #: Streamed imports of the installation right now: ``{"active", "waiting", "limit"}``.
    imports: dict[str, int] = {}


class SourceModelSummary(BaseModel):
    id: str
    name: str


class SourceModelPage(BaseModel):
    """One page as the SOURCE cut it — its size and its totals, never ours."""

    total: int | None
    page: int
    pages: int | None
    has_next: bool
    has_previous: bool
    models: list[SourceModelSummary]


class SourceFile(BaseModel):
    id: str
    name: str
    mime: str
    importable: bool
    filename: str | None = None
    size: int | None = None
    presupported: bool | None = None
    available: bool = True


class ImportedEntry(BaseModel):
    library_file_id: int
    folder_id: int | None
    filename: str


class SourceModel(BaseModel):
    id: str
    name: str
    caption: str | None = None
    description: str | None = None
    license: str | None = None
    creator: str | None = None
    tags: list[str] = []
    sensitive: bool = False
    url: str | None = None
    has_preview: bool = False
    files: list[SourceFile] = []
    #: file id -> the library file that already holds it (only files the caller may see).
    already_imported: dict[str, ImportedEntry] = {}


class SourceImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_id: str = Field(min_length=1, max_length=64)
    file_id: str = Field(min_length=1, max_length=64)
    folder_id: int | None = None


class SourceImportResponse(BaseModel):
    library_file_id: int
    filename: str
    folder_id: int | None
    was_existing: bool


def refusal(exc: ProviderError) -> HTTPException:
    """A source's failure as OUR answer. The frontend reacts to ``error``; ``message`` is the fallback text."""
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if status is None:
        if isinstance(exc, ProviderUrlError):
            status, code = 400, "source_refused"
        elif isinstance(exc, ProviderNotFoundError):
            status, code = 404, "source_not_found"
        else:
            status, code = 502, "source_failed"
    return HTTPException(status_code=status, detail={"error": code, "message": str(exc)})


def _import_name(info, file_id: str, source: str) -> str:
    """The library name of an imported file: the source's own base name when the library takes it."""
    raw = os.path.basename((info.suggested_filename or "").replace("\\", "/")).strip()
    try:
        validate_print_filename(raw)
        if len(raw) <= 255:
            return raw
    except InvalidFilenameError:
        pass
    # A name a printer's card would refuse (or none): a neutral one with the file's own ending.
    lowered = raw.lower()
    ending = next((ext for ext in manyfold_service.IMPORTABLE_EXTENSIONS if lowered.endswith(ext)), "")
    return f"{source}-{file_id}{ending}"


def make_source_router(provider: ModelProvider) -> APIRouter:
    """The catalogue routes of one provider, under its own permissions."""
    if provider.view_permission is None or provider.import_permission is None:
        raise ValueError(f"Model provider {provider.source_type!r} declares no permissions")
    source = provider.source_type
    router = APIRouter(prefix=f"/model-sources/{source}", tags=["model-sources"])
    view = provider.view_permission
    importing = provider.import_permission

    async def service_for(db: AsyncSession, user: User | None) -> ProviderService:
        service = await provider.build_service(db=db, user=user)
        if not isinstance(service, BrowsableService):
            await service.close()
            raise HTTPException(status_code=404, detail="This model source has no catalogue")
        return service

    @router.get("/status", response_model=SourceStatus)
    async def status(db: AsyncSession = Depends(get_db), current_user: User | None = RequirePermission(view)):
        service = await service_for(db, current_user)
        try:
            state = await service.get_status(db)
        finally:
            await service.close()
        return SourceStatus(
            source=source,
            name=provider.display_name,
            configured=state.configured,
            usable=state.can_download,
            base_url=getattr(service, "_base", None) or None,
            imports=model_import.import_load(),
        )

    @router.get("/models", response_model=SourceModelPage)
    async def list_models(
        q: str = Query(default="", max_length=200),
        page: int = Query(default=1, ge=1, le=1_000_000),
        db: AsyncSession = Depends(get_db),
        current_user: User | None = RequirePermission(view),
    ):
        """One page of the source's models, searched and paged BY the source."""
        service = await service_for(db, current_user)
        try:
            return SourceModelPage(**await service.list_models(query=q, page=page))
        except ProviderError as exc:
            raise refusal(exc) from exc
        finally:
            await service.close()

    @router.get("/models/{model_id}", response_model=SourceModel)
    async def get_model(
        model_id: str,
        request: Request,
        db: AsyncSession = Depends(get_db),
        current_user: User | None = RequirePermission(view),
    ):
        service = await service_for(db, current_user)
        try:
            model = await service.get_model(model_id)
        except ProviderError as exc:
            raise refusal(exc) from exc
        finally:
            await service.close()

        # Which of its files the library already holds — only rows the caller may see.
        from backend.app.api.routes.library import library_file_name_visible
        from backend.app.api.routes.library_thumbnail_jobs import request_authority

        access = await read_access(db, authority=await request_authority(request, current_user))
        rows = await db.execute(
            select(LibraryFile.id, LibraryFile.source_url, LibraryFile.folder_id, LibraryFile.filename)
            .where(
                LibraryFile.source_type == source,
                provider.source_url_filter(LibraryFile.source_url, model["id"]),
                LibraryFile.deleted_at.is_(None),
                library_file_name_visible(access.scope, access.user_id),
            )
            .order_by(LibraryFile.id)
        )
        already: dict[str, ImportedEntry] = {}
        for library_file_id, source_url, folder_id, filename in rows.all():
            try:
                file_id = provider.parse_url(source_url or "").sub_id
            except ProviderError:
                continue
            if file_id:
                already.setdefault(
                    file_id, ImportedEntry(library_file_id=library_file_id, folder_id=folder_id, filename=filename)
                )
        return SourceModel(
            **{key: model.get(key) for key in ("id", "name", "caption", "description", "license", "creator", "url")},
            tags=model.get("tags") or [],
            sensitive=bool(model.get("sensitive")),
            has_preview=bool(model.get("preview_file_id")),
            files=[SourceFile(**entry) for entry in model.get("files") or []],
            already_imported=already,
        )

    @router.get("/models/{model_id}/preview")
    async def preview(
        model_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: User | None = Depends(require_media_permission(view)),
    ):
        """The model's picture, fetched from the configured source by model id.

        An ``<img>`` cannot send headers: a media token in ``?token=`` (or the ordinary headers)
        under this source's view permission. No address from the client is ever fetched, and the
        bytes are served only when they start like an image.
        """
        service = await service_for(db, current_user)
        try:
            data, content_type = await service.fetch_preview(model_id)
        except ProviderError as exc:
            raise refusal(exc) from exc
        finally:
            await service.close()
        return Response(
            content=data,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    @router.post("/import", response_model=SourceImportResponse)
    async def import_file(
        body: SourceImportRequest,
        request: Request,
        db: AsyncSession = Depends(get_db),
        current_user: User | None = RequirePermission(importing),
    ):
        """Download one file of a model into the library (or answer with the row that holds it)."""
        from backend.app.api.routes.library_thumbnail_jobs import request_authority

        authority = await request_authority(request, current_user)
        folder = await model_import.destination_folder(db, provider, body.folder_id, authority)
        service = await service_for(db, current_user)
        try:
            frozen = getattr(service, "_config", None)

            async def connection_unchanged(check_db: AsyncSession, _principal) -> None:
                # The download may have taken minutes: the connection the bytes came through must
                # still be the installation's when they are published.
                if frozen is None:
                    return
                current = await manyfold_config.load_config(check_db)
                if not current.enabled or current.destination != frozen.destination:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "source_connection_changed",
                            "message": "The connection to the model source changed during the import.",
                        },
                    )

            async def fresh_authority():
                return await request_authority(request, current_user)

            try:
                imported = await model_import.import_resource(
                    db,
                    provider=provider,
                    service=service,
                    ref=ProviderResourceRef(source_type=source, external_id=body.model_id, sub_id=body.file_id),
                    folder=folder,
                    authority=authority,
                    fresh_authority=fresh_authority,
                    created_by_id=current_user.id if current_user else None,
                    filename_for=lambda info: _import_name(info, body.file_id, source),
                    publication_check=connection_unchanged,
                    origin_of=getattr(service, "origin_of", None),
                )
            except ProviderError as exc:
                raise refusal(exc) from exc
        finally:
            await service.close()
        return SourceImportResponse(
            library_file_id=imported.file.id,
            filename=imported.file.filename,
            folder_id=imported.file.folder_id,
            was_existing=imported.was_existing,
        )

    return router


manyfold_router = make_source_router(manyfold_provider)


# ---------------------------------------------------------------------------
# The installation's Manyfold connection
# ---------------------------------------------------------------------------

connection_router = APIRouter(prefix="/model-sources/manyfold/connection", tags=["model-sources"])

_SECRET = manyfold_config.CLIENT_SECRET_KEY


class ManyfoldConnection(BaseModel):
    enabled: bool
    url: str
    client_id: str
    #: Presence only. The secret itself is never returned.
    client_secret_configured: bool


class ManyfoldConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    url: str | None = Field(default=None, max_length=manyfold_config.MAX_URL_LENGTH)
    client_id: str | None = Field(default=None, max_length=manyfold_config.MAX_CLIENT_ID_LENGTH)
    #: Write-only: omitted keeps the stored secret, a value replaces it.
    client_secret: str | None = Field(default=None, max_length=manyfold_config.MAX_CLIENT_SECRET_LENGTH)
    #: ``{"manyfold_client_secret": "keep" | "clear" | "replace"}``.
    secret_actions: dict[str, str] | None = None


class ManyfoldConnectionTest(BaseModel):
    ok: bool
    #: How many models the application sees (None when Manyfold does not say).
    models: int | None = None
    error: str | None = None
    message: str | None = None


def _presence(values: dict) -> ManyfoldConnection:
    config = manyfold_config.from_values(values)
    return ManyfoldConnection(
        enabled=config.enabled,
        url=config.url,
        client_id=config.client_id,
        client_secret_configured=bool(config.client_secret),
    )


async def _resolve_edit(db: AsyncSession, body: ManyfoldConnectionUpdate) -> dict:
    """The connection this edit asks for, resolved under the credential anchor. Writes nothing.

    The caller commits (save) or rolls back (draft test) BEFORE any network I/O.
    """
    from backend.app.core.credential_config import canonical_url, lock_credential_config, merge_secret_fields
    from backend.app.services.model_providers.outbound import PinnedOrigin

    await lock_credential_config(db)
    stored = await manyfold_config.stored_values(db)
    changes: dict[str, str] = {}
    if body.enabled is not None:
        changes[manyfold_config.ENABLED_KEY] = "true" if body.enabled else "false"
    if body.url is not None:
        url = body.url.strip().rstrip("/")
        if url:
            try:
                PinnedOrigin.admit(url, label="Manyfold URL")
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        changes[manyfold_config.URL_KEY] = url
    if body.client_id is not None:
        changes[manyfold_config.CLIENT_ID_KEY] = body.client_id.strip()
    if body.client_secret is not None:
        changes[_SECRET] = body.client_secret

    def destination(values) -> tuple[str, str]:
        return (
            canonical_url(values.get(manyfold_config.URL_KEY)),
            str(values.get(manyfold_config.CLIENT_ID_KEY) or ""),
        )

    # A kept secret belongs to the address and client id it was entered for: changing either
    # without replacing or clearing it is refused here (400), in the save and in the draft test.
    merged = merge_secret_fields(stored, changes, {_SECRET}, body.secret_actions, scope=destination)
    if manyfold_config.from_values(merged).enabled and not manyfold_config.from_values(merged).configured:
        raise HTTPException(
            status_code=400, detail="Manyfold needs its URL, client ID and client secret before it is switched on"
        )
    return merged


@connection_router.get("", response_model=ManyfoldConnection)
async def get_connection(
    db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.SETTINGS_READ)
):
    return _presence(await manyfold_config.stored_values(db))


@connection_router.put("", response_model=ManyfoldConnection)
async def save_connection(
    body: ManyfoldConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    from backend.app.core.db_dialect import upsert_setting
    from backend.app.models.settings import Settings

    merged = await _resolve_edit(db, body)
    for key in manyfold_config.KEYS:
        await upsert_setting(db, Settings, key, str(merged.get(key) or ""))
    await db.commit()
    manyfold_service.clear_caches()
    return _presence(merged)


@connection_router.post("/test", response_model=ManyfoldConnectionTest)
async def test_connection(
    body: ManyfoldConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Try the connection the form shows — saved values where the form sent none — and save nothing.

    Reads the instance's capabilities (is it Manyfold, and new enough), signs in, and reads the
    first page of models.
    """
    merged = await _resolve_edit(db, body)
    await db.rollback()  # the anchor is released before the network
    values = {**merged, manyfold_config.ENABLED_KEY: "true"}
    config = manyfold_config.from_values(values)
    if not config.configured:
        return ManyfoldConnectionTest(
            ok=False, error="manyfold_not_configured", message="Enter the URL, the client ID and the client secret."
        )
    service = manyfold_service.ManyfoldService(config)
    try:
        result = await service.check()
    except ProviderError as exc:
        return ManyfoldConnectionTest(ok=False, error=getattr(exc, "code", "manyfold_failed"), message=str(exc))
    finally:
        await service.close()
    return ManyfoldConnectionTest(ok=True, models=result["models"])
