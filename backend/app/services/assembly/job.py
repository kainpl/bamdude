"""The background job of "Assemble project": prepare each printer row, stage, pack in the service, publish.

    route  → validates, captures authority and the sources' proofs, enqueues a slice-dispatch job (kind "assembly")
    run()  → 1. flatten the presets of every printer row and compose its settings (main; no slicer sidecar)
             2. stage a private directory: the source bytes, the settings, job.json
             3. hand it to the assembly microservice and wait (the queue and the packing child are its own)
             4. publish every output through the library ingest, the sources' proofs re-read in the same
                transaction; a re-layout then moves the previous files to the trash
             5. remove the staging directory

Meshes are never parsed here. A job always ENDS with a result the dialog can show: ``outcome`` "ok" or "failed"
with a closed ``reason``, and per printer row its own status — a row with an unusable preset fails alone, by name.
Only a cancel (the dispatcher's 499) and a bug end the dispatcher's job as failed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import HTTPException

from backend.app.core.permissions import Permission
from backend.app.schemas.assembly import MAX_SOURCE_BYTES, AssemblyLayout, AssemblyPrinter
from backend.app.services.assembly import record as rec
from backend.app.services.assembly.presets import PresetUnresolved, flatten
from backend.app.services.assembly.runtime import AssemblyFailed, assembly_staging, run_assembly
from backend.app.services.assembly.settings import compose_project_settings
from backend.app.services.assembly.zone import GeometryUnknown, OriginUnsupported, printer_geometry, work_zone
from backend.app.services.library_identity import LibraryIdentity
from backend.app.services.preview_artifacts import disk
from backend.app.utils.filename import safe_path_component

logger = logging.getLogger(__name__)

PICTURE_BYTES = 4 * 1024 * 1024  # all plate pictures of one job, inlined into its result
OUTPUT_BYTES = 512 * 1024 * 1024  # one written 3MF
JOB_BYTES = 256 * 1024
MAX_ROW_FILES = 512  # a file per plate: more plates than this is not a layout anybody asked for
CAD_FORMATS = ("step", "stp")
#: A free name is chosen and its row written under this lock: the ingest's own choice is check-then-write, and
#: two assemblies saving "jar.3mf" into one folder at the same moment would both find the name free.
_PUBLISH_LOCK = asyncio.Lock()
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
#: reasons after which no further printer row is published
_STOPS_PUBLICATION = frozenset({"source_changed", "not_allowed", "restore_in_progress"})


class SourceChanged(Exception):
    """The bytes on disk are not the ones the library row was chosen for."""


@dataclass(frozen=True)
class Source:
    """One library file the job reads, with the proof that it is still the file the caller chose."""

    proof: LibraryIdentity  # the row the caller chose (A32)
    path: Path
    fmt: str  # "stl" | "obj" | "step" | "stp" for a new assembly, "3mf" for a re-layout
    name: str


@dataclass(frozen=True)
class Plan:
    mode: Literal["new", "relayout"]
    name: str
    parts: tuple[dict, ...]  # {"name", "quantity", "fill", "orientation"}, in object order
    sources: tuple[Source, ...]  # new: one per part; relayout: the files of the set
    printers: tuple[AssemblyPrinter, ...]
    layout: AssemblyLayout
    folder_id: int | None
    authority: object  # the request's authority; re-proved before every publication
    owner_id: int | None  # the acting user, for attribution
    cloud_user_id: int | None  # whose cloud presets may be read
    trash_ids: tuple[int, ...] = ()  # re-layout: the previous files the caller may delete


@dataclass
class Row:
    key: str
    request: AssemblyPrinter
    label: str
    model: str | None = None
    status: str = "ok"  # ok | failed | partial
    reason: str = ""
    detail: str = ""
    zone: object = None
    bed_size: tuple = ()
    settings: bytes = b""
    parts: list = field(default_factory=list)
    files: list = field(default_factory=list)
    cleanup_proofs: dict = field(default_factory=dict, repr=False)
    cleanup_authority: object = field(default=None, repr=False)
    plates: list = field(default_factory=list)
    fill_counts: dict = field(default_factory=dict)
    fill_limited: list = field(default_factory=list)

    def fail(self, reason: str, detail: str = "") -> None:
        self.status = "partial" if self.files else "failed"
        self.reason, self.detail = reason, detail

    def result(self) -> dict:
        return {
            "label": self.label,
            "model": self.model,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "parts": self.parts,
            "files": self.files,
            "plates": self.plates,
            "fill_counts": self.fill_counts,
            "fill_limited": self.fill_limited,
        }


def read_record(path: Path) -> rec.AssemblyRecord:
    """The input record of a project assembled here. ``ValueError`` for any other file."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "Metadata/platenest.json" not in names or rec.RECORD_MEMBER not in names:
                raise ValueError("not an assembly record: no record in the file")
            with archive.open(rec.RECORD_MEMBER) as member:
                return rec.load(member.read(rec.MAX_RECORD_BYTES + 1))
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("not an assembly record: unreadable file") from error


def _result(rows: list[Row], *, outcome: str = "ok", reason: str = "", parts: list | None = None) -> dict:
    return {
        "outcome": outcome,
        "reason": reason,
        "parts": parts or [],
        "printers": [row.result() for row in rows],
        "trashed": [],
        "removed": [],
        "kept": [],
    }


def _reason_of(error: HTTPException) -> str:
    detail = error.detail
    if isinstance(detail, dict):
        code = detail.get("code") or detail.get("error")
        if isinstance(code, str) and _WORD.fullmatch(code):
            return code
    if error.status_code in (401, 403):
        return "not_allowed"
    if error.status_code == 503:
        return "restore_in_progress"
    return "publish_failed"


async def _prepare_rows(plan: Plan) -> tuple[list[Row], list]:
    """Step 1: every printer row's presets, zone and settings. A row that cannot be prepared fails alone."""
    from backend.app.api.routes.library import _canonical_printer_model
    from backend.app.core.database import async_session
    from backend.app.services.domain_authority import fresh

    snapshots: list = []
    rows: list[Row] = []
    async with async_session() as db:
        principal = await fresh(db, plan.authority, permissions=(Permission.LIBRARY_UPLOAD.value,))
        user = principal.user if principal is not None and plan.cloud_user_id == principal.user.id else None
        for index, request in enumerate(plan.printers):
            row = Row(key=f"p{index}", request=request, label=request.printer_preset.id)
            rows.append(row)
            try:
                printer = await flatten(db, user, request.printer_preset, "printer", credential_snapshots=snapshots)
                row.label = printer.name
                row.model = _canonical_printer_model(printer.flat.get("printer_model") or printer.name)
                process = await flatten(db, user, request.process_preset, "process", credential_snapshots=snapshots)
                filament = await flatten(db, user, request.filament_preset, "filament", credential_snapshots=snapshots)
                geometry = printer_geometry(printer.flat)
                try:
                    row.zone = work_zone(geometry, request.nozzle)
                except ValueError:
                    row.fail("nozzle_unknown", str(request.nozzle))
                    await db.commit()
                    continue
                row.bed_size = geometry.bed_size
                row.settings = compose_project_settings(
                    process,
                    filament,
                    printer,
                    nozzle=request.nozzle,
                    nozzle_count=len(geometry.nozzles),
                    bed_type=request.bed_type,
                )
                await db.commit()  # the walker cached the parents it fetched
            except PresetUnresolved as error:
                await db.rollback()
                row.fail("preset_unresolved", f"{error.name} ({error.missing})")
            except HTTPException:
                # the resolver's own refusal (unknown id, no right to that cloud preset); its sentence is for an
                # HTTP answer in the system language, so the row carries the closed reason alone
                await db.rollback()
                row.fail("preset_unresolved")
            except GeometryUnknown as error:
                await db.rollback()
                row.fail("origin_unsupported" if isinstance(error, OriginUnsupported) else "geometry_unknown")
            except Exception as error:  # a malformed preset must fail its own row, by name, not the whole job
                await db.rollback()
                logger.warning("Assembly preset unusable: error=%s", type(error).__name__)
                row.fail("preset_invalid")
    return rows, snapshots


def _stage(root: Path, plan: Plan, rows: list[Row]) -> None:
    """Step 2, in a worker thread: the job's private directory."""
    (root / "src").mkdir(parents=True)
    sources = []
    for index, source in enumerate(plan.sources):
        # a STEP file goes to the service's converter, and the packer is told the mesh that will be left there
        cad = source.fmt in CAD_FORMATS
        member = f"step/{index}/input.step" if cad else f"src/{index}.{source.fmt}"
        if cad:
            (root / "step" / str(index)).mkdir(parents=True)
        # the row was chosen by its hash; an external file may have changed on its share since the last scan
        digest, copied = hashlib.sha256(), 0
        with source.path.open("rb") as reader, (root / member).open("wb") as writer:
            while block := reader.read(1024 * 1024):
                copied += len(block)
                if copied > MAX_SOURCE_BYTES:
                    raise ValueError("source larger than the request allowed")
                digest.update(block)
                writer.write(block)
        if source.proof.sha256 and digest.hexdigest() != source.proof.sha256:
            raise SourceChanged(source.name)
        sources.append(
            {"member": f"step/{index}/mesh.stl", "format": "stl"} if cad else {"member": member, "format": source.fmt}
        )
    printers = []
    for row in rows:
        (root / f"{row.key}.settings.json").write_bytes(row.settings)
        zone = row.zone
        printers.append(
            {
                "key": row.key,
                "label": row.label,
                "bed_size": [float(row.bed_size[0]), float(row.bed_size[1])],
                "zone": {
                    "width": zone.width,
                    "depth": zone.depth,
                    "allowed": [[list(point) for point in polygon] for polygon in zone.allowed],
                    "excluded": [[list(point) for point in polygon] for polygon in zone.excluded],
                    "max_height": zone.max_height,
                },
                "settings": f"{row.key}.settings.json",
                "record_printer": {
                    "printer": row.request.printer_preset.model_dump(),
                    "process": row.request.process_preset.model_dump(),
                    "filament": row.request.filament_preset.model_dump(),
                    "nozzle": row.request.nozzle,
                    "model": row.model or "",
                    "bed_type": row.request.bed_type,
                },
            }
        )
    job = json.dumps(
        {
            "mode": plan.mode,
            "sources": sources,
            "parts": list(plan.parts),
            "layout": plan.layout.model_dump(),
            "base_name": plan.name,
            "printers": printers,
        }
    ).encode("utf-8")
    if len(job) > JOB_BYTES:
        raise ValueError("job description too large")
    (root / "job.json").write_bytes(job)


def _read_member(root: Path, member: object, pattern: re.Pattern[str], limit: int) -> bytes | None:
    """One output file of the child, by a name of the expected form, inside the job's directory, under a cap."""
    path = _member_path(root, member, pattern, limit)
    try:
        return path.read_bytes() if path is not None else None
    except OSError:
        return None


def _member_path(root: Path, member: object, pattern: re.Pattern[str], limit: int) -> Path | None:
    if not isinstance(member, str) or not pattern.fullmatch(member):
        return None
    path = (root / member).resolve()
    try:
        path.relative_to(root.resolve())
        if not path.is_file() or path.stat().st_size > limit:
            return None
    except (OSError, ValueError):
        return None
    return path


def _counts(value: object, parts: int) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(index): copies
        for index, copies in value.items()
        # str.isdigit() also accepts characters int() refuses (a superscript two): ASCII digits only
        if isinstance(index, str)
        and index.isascii()
        and index.isdigit()
        and int(index) < parts
        and type(copies) is int
        and copies > 0
    }


def _output_pattern(row: Row) -> re.Pattern[str]:
    return re.compile(rf"out/{re.escape(row.key)}(_\d+)?\.3mf")


def _take_packed(plan: Plan, row: Row, packed: object, root: Path, pictures: list[int]) -> list[tuple[str, int | None]]:
    """What the child reports for one row, taken with nothing trusted: the 3MFs to publish as (member, plate).

    Names only — each file is read right before its own publication, so main never holds every output of
    every printer at once, and a result that lists one file a thousand times reads it not even once.
    """
    count = len(plan.parts)
    if not isinstance(packed, dict) or packed.get("status") not in ("ok", "failed"):
        row.fail("bad_result")
        return []
    if packed["status"] == "failed":
        reason = packed.get("reason")
        row.fail(reason if isinstance(reason, str) and _WORD.fullmatch(reason) else "bad_result")
        for part in packed.get("parts") if isinstance(packed.get("parts"), list) else []:
            if isinstance(part, dict) and type(part.get("index")) is int and 0 <= part["index"] < count:
                why = part.get("reason")
                row.parts.append(
                    {
                        "index": part["index"],
                        "name": plan.parts[part["index"]]["name"],
                        "reason": why if isinstance(why, str) and _WORD.fullmatch(why) else "",
                    }
                )
        return []

    outputs: list[tuple[str, int | None]] = []
    key = re.escape(row.key)
    listed = packed.get("files") if isinstance(packed.get("files"), list) else []
    allowed = MAX_ROW_FILES if plan.layout.split_plates else 1
    pattern = _output_pattern(row)
    for entry in listed if 0 < len(listed) <= allowed else []:
        plate = entry.get("plate") if isinstance(entry, dict) else None
        member = entry.get("member") if isinstance(entry, dict) else None
        if (
            _member_path(root, member, pattern, OUTPUT_BYTES) is None
            or any(member == taken for taken, _ in outputs)
            or not (plate is None or (type(plate) is int and plate >= 0))
        ):
            row.fail("bad_result")
            return []
        outputs.append((member, plate))
    if not outputs:
        row.fail("bad_result")
        return []

    for plate in packed.get("plates") if isinstance(packed.get("plates"), list) else []:
        if not isinstance(plate, dict) or type(plate.get("plate")) is not int:
            continue
        picture = None
        raw = _read_member(root, plate.get("picture"), re.compile(rf"out/{key}_plate_\d+\.png"), PICTURE_BYTES)
        if raw is not None and pictures[0] + len(raw) <= PICTURE_BYTES:
            pictures[0] += len(raw)
            picture = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        row.plates.append(
            {
                "plate": plate["plate"],
                "picture": picture,
                "counts": _counts(plate.get("counts"), count),
                "fill": plate.get("fill") is True,
            }
        )
    row.fill_counts = _counts(packed.get("fill_counts"), count)
    limited = packed.get("fill_limited")
    row.fill_limited = [i for i in limited if type(i) is int and 0 <= i < count] if isinstance(limited, list) else []
    return outputs


_PLATE_WORD = {"uk": "стіл"}


def _file_name(plan: Plan, row: Row, plate: int | None) -> str:
    base = f"{plan.name} — {row.model or row.label}"
    if plate is not None:
        from backend.app.i18n import current_language

        base += f" — {_PLATE_WORD.get(current_language(), 'plate')} {plate + 1}"
    return safe_path_component(base, fallback="assembly", max_bytes=200) + ".3mf"


async def _check_sources(db, plan: Plan) -> None:
    """Every source is still the file the caller chose, and still theirs to read. 409 ``source_changed`` if not."""
    from backend.app.models.library import LibraryFile
    from backend.app.services.library_ingest import read_access

    access = await read_access(db, authority=plan.authority)
    for source in plan.sources:
        row = await db.scalar(
            LibraryFile.active().where(LibraryFile.id == source.proof.id).execution_options(populate_existing=True)
        )
        if not source.proof.matches(row):
            raise HTTPException(409, {"code": "source_changed"})
        access.ensure(row)


async def _withdraw(row: Row) -> None:
    """Take back what a row published before it failed: a printer gets all its files or none. Half a
    "file per plate" set would look like a project and could never be laid out again."""
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile
    from backend.app.services.domain_authority import guard
    from backend.app.services.library_delete_cleanup import finish
    from backend.app.services.library_delete_scope import library_delete_scope
    from backend.app.services.library_trash import library_trash_service

    remaining = []
    for entry in row.files:
        proof = row.cleanup_proofs.get(entry["library_file_id"])
        if proof is None:
            remaining.append(entry)
            continue
        try:
            async with async_session() as db:
                current = await db.get(LibraryFile, proof.id)
                if current is None:
                    continue
                if not proof.matches(current):
                    remaining.append(entry)
                    continue
                async with library_delete_scope(
                    db,
                    [proof],
                    first=lambda session: guard(
                        session, row.cleanup_authority, permissions=(Permission.LIBRARY_UPLOAD.value,)
                    ),
                ):
                    current = await db.get(LibraryFile, proof.id, populate_existing=True)
                    await library_trash_service.purge_rows(db, current)
                    await db.commit()
                await finish(db)
        except Exception as error:
            logger.warning("Assembly withdrawal failed: error=%s", type(error).__name__)
            remaining.append(entry)
    row.files[:] = remaining
    if not remaining:
        row.status = "failed"


async def _publish(plan: Plan, row: Row, outputs: list[tuple[str, int | None]], root: Path, snapshots: list) -> None:
    """Step 4 for one row: each output through the library ingest, read from the job's directory right before
    it is saved. A failure takes the row's earlier files back (``_withdraw``)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.api.routes.library import save_3mf_bytes_to_library
    from backend.app.core.database import async_session
    from backend.app.core.websocket import ws_manager
    from backend.app.models.library import LibraryFolder

    async def check(db, _principal):
        await _check_sources(db, plan)

    for member, plate in outputs:
        content = await disk(_read_member, root, member, _output_pattern(row), OUTPUT_BYTES)
        if content is None:
            row.fail("bad_result")
            await _withdraw(row)
            return
        try:
            async with _PUBLISH_LOCK, async_session() as db:
                folder = None
                fallback = None
                if plan.folder_id is not None:
                    folder = await db.scalar(
                        select(LibraryFolder)
                        .where(LibraryFolder.id == plan.folder_id)
                        .options(selectinload(LibraryFolder.products))
                    )
                    if folder is None:
                        fallback = "folder_missing"  # deleted while the job ran: the file goes to the library's root
                saved = await save_3mf_bytes_to_library(
                    db,
                    content=content,
                    filename=_file_name(plan, row, plate),
                    folder=folder,
                    created_by_id=plan.owner_id,
                    source_type="assembled",
                    authority=plan.authority,
                    credential_snapshots=snapshots,
                    publication_check=check,
                    destination="unique",
                    swap_origin="generated",
                )
                entry = {
                    "library_file_id": saved.file.id,
                    "name": saved.file.filename,
                    "plate": plate,
                    "fallback_reason": saved.fallback_reason or fallback,
                }
                row.cleanup_proofs[saved.file.id] = LibraryIdentity.capture(saved.file)
                row.cleanup_authority = plan.authority
        except HTTPException as error:
            row.fail(_reason_of(error))
            await _withdraw(row)
            return
        except Exception as error:  # the disk, the database: the type only
            logger.warning("Assembly publication failed: error=%s", type(error).__name__)
            row.fail("publish_failed")
            await _withdraw(row)
            return
        row.files.append(entry)
        try:
            await ws_manager.send_library_file_added({"id": entry["library_file_id"], "filename": entry["name"]})
        except Exception:
            pass


async def _delete_rights(db, authority, proof) -> tuple[bool, int | None]:
    """What the caller may delete NOW: (every file, the user whose own files). The route asked when the job
    was queued; minutes later the answer may have changed."""
    from backend.app.services import domain_authority

    held = []
    for permission in (Permission.LIBRARY_DELETE_ALL, Permission.LIBRARY_DELETE_OWN):
        try:
            principal = await domain_authority.fresh(db, authority, permissions=(permission.value,), proof=proof)
        except HTTPException as error:
            if error.status_code != 403:
                raise
            held.append(None)
            continue
        user = getattr(principal, "user", None)
        held.append((True, user.id if user is not None else None))
        if permission is Permission.LIBRARY_DELETE_ALL:
            return held[0]
    return (False, held[1][1]) if held[1] else (False, None)


def _may_delete(file, rights: tuple[bool, int | None]) -> bool:
    every, own = rights
    return every or (own is not None and file.created_by_id == own)


async def _trash_previous(plan: Plan) -> tuple[list[int], list[int], list[int]]:
    """A re-layout's last step: the previous files leave the library — (trashed, removed, kept). A managed
    file goes to the trash; an external one has no trash (its row goes, its bytes stay on the share). One a
    printing job uses, or one the caller may no longer delete, is kept."""
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.domain_authority import guard
    from backend.app.services.library_trash import library_trash_service

    trashed: list[int] = []
    removed: list[int] = []
    kept: list[int] = []
    from backend.app.services.library_delete_cleanup import finish
    from backend.app.services.library_delete_scope import library_delete_scope

    proofs = {source.proof.id: source.proof for source in plan.sources}
    for file_id in plan.trash_ids:
        proof = proofs.get(file_id)
        if proof is None:
            kept.append(file_id)
            continue
        try:
            async with async_session() as db:
                current = await db.get(LibraryFile, file_id)
                if current is None:
                    continue
                if not proof.matches(current):
                    kept.append(file_id)
                    continue
                actor_proof = None

                async def first(session):
                    nonlocal actor_proof
                    actor_proof, _principal = await guard(
                        session, plan.authority, permissions=(Permission.LIBRARY_UPLOAD.value,)
                    )

                async with library_delete_scope(db, [proof], first=first):
                    rights = (
                        (True, None)
                        if plan.authority is None
                        else await _delete_rights(db, plan.authority, actor_proof)
                    )
                    file = await db.get(LibraryFile, file_id, populate_existing=True)
                    printing = await db.scalar(
                        select(PrintQueueItem.id)
                        .where(PrintQueueItem.library_file_id == file_id, PrintQueueItem.status == "printing")
                        .limit(1)
                    )
                    if not _may_delete(file, rights) or printing is not None:
                        kept.append(file_id)
                        await db.rollback()
                        continue
                    in_trash = await library_trash_service.trash_or_purge(db, file)
                    await db.commit()
                await finish(db)
                (trashed if in_trash else removed).append(file_id)
        except Exception as error:
            logger.warning("Assembly previous-file cleanup retained a file: error=%s", type(error).__name__)
            kept.append(file_id)
    return trashed, removed, kept


async def run(job_id: int, plan: Plan) -> dict:
    """The job's body for the slice dispatcher. Returns the result the dialog shows (see the module docstring)."""
    result = await _run(job_id, plan)
    rows = result["printers"]
    logger.info(
        "Assembly job=%s result outcome=%s reason=%s files=%d rows_ok=%d rows_other=%d",
        job_id,
        result["outcome"],
        result["reason"],
        sum(len(row["files"]) for row in rows),
        sum(row["status"] == "ok" for row in rows),
        sum(row["status"] != "ok" for row in rows),
    )
    return result


async def _run(job_id: int, plan: Plan) -> dict:
    from backend.app.core.database import async_session
    from backend.app.services.slice_dispatch import slice_dispatch

    slice_dispatch.set_progress(job_id, {"stage": "preparing"})
    try:
        rows, snapshots = await _prepare_rows(plan)
    except HTTPException as error:
        return _result([], outcome="failed", reason=_reason_of(error))
    good = [row for row in rows if row.status == "ok"]
    if not good:
        return _result(rows, outcome="failed", reason="no_printer")
    staging = assembly_staging()
    if staging is None:
        return _result(rows, outcome="failed", reason="unavailable")

    root = staging / f"{job_id}-{uuid.uuid4().hex}"
    try:
        try:
            await disk(_stage, root, plan, good)
        except FileNotFoundError:
            return _result(rows, outcome="failed", reason="source_missing")
        except SourceChanged:
            return _result(rows, outcome="failed", reason="source_changed")
        except (OSError, ValueError) as error:
            logger.warning("Assembly job=%s staging failed: error=%s", job_id, type(error).__name__)
            return _result(rows, outcome="failed", reason="staging_failed")
        try:
            packed = await run_assembly(
                root,
                job_id=job_id,
                on_progress=lambda progress: slice_dispatch.set_progress(job_id, progress),
                finish_requested=lambda: bool((job := slice_dispatch.get(job_id)) is not None and job.finish_requested),
            )
        except AssemblyFailed as failed:
            reason = failed.why if failed.outcome == "failed" and failed.why else failed.outcome
            parts = [
                {"index": part["index"], "name": plan.parts[part["index"]]["name"], "reason": part.get("reason") or ""}
                for part in failed.parts
                if isinstance(part, dict)
                and type(part.get("index")) is int
                and 0 <= part["index"] < len(plan.parts)
                and (
                    part.get("reason") is None or (isinstance(part["reason"], str) and _WORD.fullmatch(part["reason"]))
                )
            ]
            return _result(rows, outcome="failed", reason=reason, parts=parts)

        # From here the job cannot be stopped by its owner (the route refuses once it reads this stage): a
        # stop that lands mid-publication would leave files behind a message saying nothing was saved.
        slice_dispatch.set_progress(job_id, {"stage": "publishing"})
        by_key = {
            entry.get("key"): entry
            for entry in (packed.get("printers") if isinstance(packed.get("printers"), list) else [])
            if isinstance(entry, dict)
        }
        pictures = [0]
        taken = [(row, await disk(_take_packed, plan, row, by_key.get(row.key), root, pictures)) for row in good]
        try:
            async with async_session() as db:  # before the first publication: one changed source publishes nothing
                await _check_sources(db, plan)
        except HTTPException as error:
            return _result(rows, outcome="failed", reason=_reason_of(error))
        stopped = ""
        for row, outputs in taken:
            if row.status != "ok":
                continue
            if stopped:
                row.fail(stopped)
                continue
            await _publish(plan, row, outputs, root, snapshots)
            if row.reason in _STOPS_PUBLICATION:
                stopped = row.reason
    finally:
        await disk(shutil.rmtree, root, True)

    result = _result(rows)
    published = sum(len(row.files) for row in rows)
    if not published:
        result.update(outcome="failed", reason=stopped or "nothing_published")
    elif plan.trash_ids and all(row.status == "ok" for row in rows):
        try:
            result["trashed"], result["removed"], result["kept"] = await _trash_previous(plan)
        except Exception as error:  # the new files are saved: a failure here keeps the old ones, nothing more
            logger.warning("Assembly job=%s could not trash the previous files: error=%s", job_id, type(error).__name__)
            result["kept"] = list(plan.trash_ids)
    elif plan.trash_ids:
        result["kept"] = list(plan.trash_ids)  # not every row made it: the previous files stay
    return result
