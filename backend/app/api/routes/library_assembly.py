""" "Assemble project": lay selected library models out densely and write project 3MFs into the library.

The routes validate, capture the caller's authority and the sources' proofs, and enqueue a slice-dispatch job
(kind ``assembly``); the packing itself runs in the assembly microservice (``services/assembly/job.py``). No
slicer sidecar takes part. A source the caller cannot see and a missing one are the same 404.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import String, cast
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.cloud import resolve_api_key_cloud_owner
from backend.app.api.routes.library import _canonical_printer_model, file_name_visible, to_absolute_path
from backend.app.api.routes.library_thumbnail_jobs import request_authority
from backend.app.core.auth import (
    RequestCredentials,
    acting_user,
    library_name_scope,
    request_credentials,
    require_ownership_permission,
    require_permission,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.app.schemas.assembly import (
    MAX_COPIES,
    MAX_SOURCE_BYTES,
    AssemblyRecordResponse,
    AssemblyRequest,
    PrinterInfo,
    RelayoutRequest,
)
from backend.app.schemas.slicer import PresetRef
from backend.app.services.assembly import job as assembly_job
from backend.app.services.assembly.presets import PresetUnresolved, flatten
from backend.app.services.assembly.zone import GeometryUnknown, OriginUnsupported, printer_geometry
from backend.app.services.preview_artifacts import disk as preview_disk
from backend.app.services.preview_protocol import STEP_PRINT_INPUT_BYTES
from backend.app.services.slicer_routing import refuse_bed_type_for_slicer

router = APIRouter(prefix="/library", tags=["library"])

_MESH_TYPES = ("stl", "obj")
_CAD_TYPES = assembly_job.CAD_FORMATS
_DELETE_GATE = require_ownership_permission(Permission.LIBRARY_DELETE_ALL, Permission.LIBRARY_DELETE_OWN)


def _suffix(filename: str | None) -> str:
    return Path(filename or "").suffix.lower().lstrip(".")


def _proof(file: LibraryFile):
    """The proof that a source is still the row the caller chose (A32): ``LibraryIdentity``, never a tuple."""
    from backend.app.services.library_identity import LibraryIdentity

    return LibraryIdentity.capture(file)


async def _visible_files(db: AsyncSession, request: Request, user: User | None, ids: list[int]):
    """The caller's actor, and the files of ``ids`` — every one visible to it, or one 404 for the whole request."""
    actor = await acting_user(request, db, user)
    scope = await library_name_scope(request, db, actor)
    rows = (await db.scalars(LibraryFile.active().where(LibraryFile.id.in_(set(ids))))).all()
    files = {row.id: row for row in rows if file_name_visible(row, actor, scope)}
    if set(ids) - set(files):
        raise HTTPException(404, "File not found")
    return actor, scope, files


def _cad_installed() -> bool:
    """Whether this server can convert a STEP file at all (the bundle of the STEP thumbnails). Blocking."""
    from backend.app import step_runtime  # stdlib-only: it never imports OCP
    from backend.app.core.config import settings

    try:
        return bool(step_runtime.locate(settings.app_dir).ok)
    except Exception:
        return False


async def _on_disk(file: LibraryFile) -> Path:
    path = to_absolute_path(file.file_path)
    if path is None or not await preview_disk(path.is_file):
        raise HTTPException(404, "Source file missing on disk")
    return path


async def _enqueue(plan: assembly_job.Plan, *, source: LibraryFile, actor: User | None, authority) -> dict:
    from backend.app.services.slice_dispatch import slice_dispatch

    async def run(job_id: int) -> dict:
        return await assembly_job.run(job_id, plan)

    job = await slice_dispatch.enqueue(
        kind="assembly",
        source_id=source.id,
        source_name=plan.name,
        owner_id=actor.id if actor else None,
        authority=authority.capture,
        run=run,
    )
    return {"job_id": job.id, "status": job.status, "status_url": f"/api/v1/slice-jobs/{job.id}"}


def _check_copies(parts) -> None:
    if sum(part.quantity for part in parts if not part.fill) > MAX_COPIES:
        raise HTTPException(400, "Too many copies: an assembly takes at most 2000 in total")


@router.post("/assemble", status_code=202)
async def assemble_project(
    body: AssemblyRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
):
    """Enqueue an assembly of the chosen STL / OBJ / STEP files. Returns 202 + job id; poll ``GET /slice-jobs/{id}``."""
    for target in body.targets:
        refuse_bed_type_for_slicer(target.bed_type, body.slicer)
    if any(part.file_id is None for part in body.parts):
        raise HTTPException(422, "Every part of a new assembly names its file")
    actor, _scope, files = await _visible_files(db, http_request, current_user, [part.file_id for part in body.parts])
    ordered = [files[part.file_id] for part in body.parts]
    for file in ordered:
        kind = _suffix(file.filename)
        if kind not in _MESH_TYPES + _CAD_TYPES:
            raise HTTPException(400, f"Only STL, OBJ and STEP files can be assembled: {file.filename}")
        if kind in _CAD_TYPES and (file.file_size or 0) > STEP_PRINT_INPUT_BYTES:
            raise HTTPException(413, f"A STEP file larger than 64 MB cannot be assembled: {file.filename}")
    if sum(file.file_size or 0 for file in files.values()) > MAX_SOURCE_BYTES:
        raise HTTPException(413, "The selected files are larger than 200 MB in total")
    _check_copies(body.parts)
    if any(_suffix(file.filename) in _CAD_TYPES for file in ordered) and not await preview_disk(_cad_installed):
        raise HTTPException(409, "STEP files cannot be assembled: STEP support is not installed on this server")
    authority = await request_authority(http_request, current_user)
    sources = [
        assembly_job.Source(_proof(file), await _on_disk(file), _suffix(file.filename), file.filename)
        for file in ordered
    ]
    cloud_user = current_user or api_key_cloud_owner
    plan = assembly_job.Plan(
        mode="new",
        name=body.name.strip() or "assembly",
        parts=tuple(
            {"name": file.filename, "quantity": part.quantity, "fill": part.fill, "orientation": part.orientation}
            for part, file in zip(body.parts, ordered, strict=True)
        ),
        sources=tuple(sources),
        printers=tuple(body.targets),
        layout=body.layout,
        folder_id=ordered[0].folder_id,
        authority=authority,
        owner_id=actor.id if actor else None,
        cloud_user_id=cloud_user.id if cloud_user else None,
    )
    await db.commit()
    return await _enqueue(plan, source=ordered[0], actor=actor, authority=authority)


@router.get("/assemble/printer-info", response_model=PrinterInfo)
async def assembly_printer_info(
    source: str = Query(...),
    id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
):
    """What a printer preset says about the machine: its model, bed and the reach of each nozzle."""
    try:
        ref = PresetRef(source=source, id=id)
    except ValidationError as error:
        raise HTTPException(422, "Unknown preset reference") from error
    try:
        printer = await flatten(db, current_user or api_key_cloud_owner, ref, "printer", credential_snapshots=[])
        await db.commit()  # the walker cached the parents it fetched
        geometry = printer_geometry(printer.flat)
    except PresetUnresolved as error:
        await db.rollback()
        raise HTTPException(
            422,
            {"error": "preset_unresolved", "message": f"The printer preset could not be resolved: {error.name}"},
        ) from None
    except OriginUnsupported:
        raise HTTPException(
            422,
            {
                "error": "origin_unsupported",
                "message": "The printer preset counts from inside its bed; projects cannot be assembled for it",
            },
        ) from None
    except GeometryUnknown:
        raise HTTPException(
            422,
            {"error": "geometry_unknown", "message": "The printer preset does not describe its print area"},
        ) from None
    return PrinterInfo(
        model=_canonical_printer_model(printer.flat.get("printer_model") or printer.name),
        bed_size=geometry.bed_size,
        nozzles=[{"index": n.index, "area": n.area, "max_height": n.max_height} for n in geometry.nozzles],
    )


async def _assembled(db: AsyncSession, request: Request, user: User | None, file_id: int):
    """An assembled file the caller sees, its record and the visible files of its set."""
    actor, scope, files = await _visible_files(db, request, user, [file_id])
    file = files[file_id]
    refusal = HTTPException(
        409, {"error": "not_assembled", "message": "This file was not assembled here, or its record is damaged"}
    )
    if not (file.file_metadata or {}).get("assembled"):
        raise refusal
    try:
        record = await preview_disk(assembly_job.read_record, await _on_disk(file))
    except ValueError:
        raise refusal from None
    # the set id is 32 hex characters: a text match narrows the rows, the metadata itself decides
    candidates = await db.scalars(
        LibraryFile.active().where(cast(LibraryFile.file_metadata, String).contains(record.set_id, autoescape=True))
    )
    members = [
        row
        for row in candidates
        if (row.file_metadata or {}).get("assembly_set") == record.set_id and file_name_visible(row, actor, scope)
    ]
    members.sort(key=lambda row: row.id)
    return actor, file, record, members


@router.get("/files/{file_id}/assembly", response_model=AssemblyRecordResponse)
async def get_assembly_record(
    file_id: int,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
):
    """The values an assembled file was made with, for the re-layout dialog."""
    _actor, _file, record, members = await _assembled(db, http_request, current_user, file_id)
    data = asdict(record)
    return AssemblyRecordResponse(
        set_id=record.set_id,
        base_name=record.base_name,
        parts=data["parts"],
        printer=data["printer"],
        layout=data["layout"],
        plates=list(record.plates),
        set_files=[{"id": row.id, "filename": row.filename} for row in members],
    )


@router.post("/files/{file_id}/relayout", status_code=202)
async def relayout_file(
    file_id: int,
    body: RelayoutRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Lay an assembled project out again from the models stored in it. Returns 202 + job id."""
    for target in body.targets:
        refuse_bed_type_for_slicer(target.bed_type, body.slicer)
    actor, file, record, members = await _assembled(db, http_request, current_user, file_id)
    if len(body.parts) != len(record.parts):
        raise HTTPException(422, "The parts do not match the assembled project")
    _check_copies(body.parts)
    expected = len(record.plates) if record.layout.split_plates else 1
    # every file of a set is another plate, so other bytes: two copies of one plate are not two plates
    distinct = len({row.file_hash or f"row:{row.id}" for row in members})
    if distinct < expected:
        raise HTTPException(
            409,
            {
                "error": "assembly_set_incomplete",
                "message": f"This project was assembled as {expected} files and only {distinct} of them are in "
                "the library",
            },
        )
    trash_ids: tuple[int, ...] = ()
    if body.trash_previous:
        deleter, can_delete_all = await creds.check(_DELETE_GATE)
        if not can_delete_all and any(deleter is None or row.created_by_id != deleter.id for row in members):
            raise HTTPException(403, "You can only delete your own files")
        trash_ids = tuple(row.id for row in members)
    authority = await request_authority(http_request, current_user)
    sources = [assembly_job.Source(_proof(row), await _on_disk(row), "3mf", row.filename) for row in members]
    cloud_user = current_user or api_key_cloud_owner
    plan = assembly_job.Plan(
        mode="relayout",
        name=body.name.strip() or record.base_name or "assembly",
        parts=tuple(
            {"name": old.name, "quantity": part.quantity, "fill": part.fill, "orientation": part.orientation}
            for part, old in zip(body.parts, record.parts, strict=True)
        ),
        sources=tuple(sources),
        printers=tuple(body.targets),
        layout=body.layout,
        folder_id=file.folder_id,
        authority=authority,
        owner_id=actor.id if actor else None,
        cloud_user_id=cloud_user.id if cloud_user else None,
        trash_ids=trash_ids,
    )
    await db.commit()
    return await _enqueue(plan, source=file, actor=actor, authority=authority)
