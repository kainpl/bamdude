"""API routes for File Manager (Library) functionality."""

import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse as FastAPIFileResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.cloud import resolve_api_key_cloud_owner
from backend.app.core.auth import (
    require_ownership_permission,
    require_permission,
)
from backend.app.core.config import settings as app_settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_project_links import library_file_projects, library_folder_projects
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.project import Project
from backend.app.models.user import User
from backend.app.schemas.library import (
    AddToQueueError,
    AddToQueueRequest,
    AddToQueueResponse,
    AddToQueueResult,
    BatchThumbnailRequest,
    BatchThumbnailResponse,
    BatchThumbnailResult,
    BulkDeleteRequest,
    BulkDeleteResponse,
    ExternalFolderCreate,
    FileDuplicate,
    FileListResponse,
    FileMoveRequest,
    FilePrintRequest,
    FileResponse as FileResponseSchema,
    FileUpdate,
    FileUploadResponse,
    FolderCreate,
    FolderResponse,
    FolderTreeItem,
    FolderUpdate,
    ProjectRef,
    ZipExtractError,
    ZipExtractResponse,
    ZipExtractResult,
)
from backend.app.services.archive import ThreeMFParser
from backend.app.services.library_helpers import compute_file_tags, detect_file_type
from backend.app.services.print_plan import inherit_folder_projects, sync_plan_for_file, sync_plan_for_folder
from backend.app.services.stl_thumbnail import generate_stl_thumbnail
from backend.app.services.threemf_capabilities import extract_3mf_capabilities
from backend.app.utils.threemf_tools import (
    extract_nozzle_mapping_from_3mf,
    extract_project_filaments_from_3mf,
    extract_source_printer_model_from_3mf,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library"])


def _project_refs(projects: list[Project]) -> list[ProjectRef]:
    """Map a list of Project ORM rows to lightweight ProjectRef DTOs."""
    return [ProjectRef(id=p.id, name=p.name, color=p.color) for p in projects]


async def _resolve_projects_for_assign(db: AsyncSession, project_ids: list[int]) -> list[Project]:
    """Validate every id in ``project_ids`` exists, return the ORM rows.

    Raises 404 with the offending id list if any are missing.
    """
    if not project_ids:
        return []
    rows = (await db.execute(select(Project).where(Project.id.in_(project_ids)))).scalars().all()
    found_ids = {p.id for p in rows}
    missing = [pid for pid in project_ids if pid not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"Project(s) not found: {missing}")
    return rows


def _clean_3mf_metadata(obj):
    """Remove non-JSON-serializable data (bytes, internal keys) from 3MF metadata."""
    if isinstance(obj, dict):
        return {
            k: _clean_3mf_metadata(v)
            for k, v in obj.items()
            if not isinstance(v, bytes) and k not in ("_thumbnail_data", "_thumbnail_ext")
        }
    elif isinstance(obj, list):
        return [_clean_3mf_metadata(i) for i in obj if not isinstance(i, bytes)]
    elif isinstance(obj, bytes):
        return None
    return obj


def get_library_dir() -> Path:
    """Get the library storage directory."""
    base_dir = Path(app_settings.archive_dir)
    library_dir = base_dir / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    return library_dir


def get_library_files_dir() -> Path:
    """Get the directory for library files."""
    files_dir = get_library_dir() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    return files_dir


def get_library_thumbnails_dir() -> Path:
    """Get the directory for library thumbnails."""
    thumbnails_dir = get_library_dir() / "thumbnails"
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    return thumbnails_dir


def to_relative_path(absolute_path: Path | str) -> str:
    """Convert an absolute path to a path relative to base_dir for storage."""
    if not absolute_path:
        return ""
    abs_path = Path(absolute_path)
    base_dir = Path(app_settings.base_dir)
    try:
        return str(abs_path.relative_to(base_dir))
    except ValueError:
        # Path is not under base_dir, return as-is (shouldn't happen normally)
        return str(abs_path)


def to_absolute_path(relative_path: str | None) -> Path | None:
    """Convert a relative path (from database) to an absolute path for file operations."""
    if not relative_path:
        return None
    # Handle already-absolute paths (for backwards compatibility during migration)
    path = Path(relative_path)
    if path.is_absolute():
        return path
    return Path(app_settings.base_dir) / relative_path


def calculate_file_hash(file_path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def _resolve_upload_destination(target_folder: LibraryFolder | None, filename: str) -> tuple[Path, bool]:
    """Resolve the on-disk destination for an uploaded file.

    Non-external target: returns ``(<library_files_dir>/<uuid><ext>, False)``.
    Writable external target: writes to ``<external_path>/<filename>``
    (preserves the real filename so the file is recognisable on the mount);
    returns ``(dest, True)``. Raises ``HTTPException`` for read-only external
    folders (403), missing/inaccessible/non-writable external paths (400), and
    filename collisions on the external mount (409). See upstream #1112 —
    previously uploads to writable external folders were silently misrouted to
    the internal library dir.
    """
    if target_folder is not None and target_folder.is_external:
        if target_folder.external_readonly:
            raise HTTPException(status_code=403, detail="Cannot upload to a read-only external folder")
        if not target_folder.external_path:
            raise HTTPException(status_code=400, detail="External folder has no configured path")
        ext_dir = Path(target_folder.external_path)
        if not ext_dir.exists() or not ext_dir.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"External path is not accessible: {target_folder.external_path}",
            )
        if not os.access(ext_dir, os.W_OK):
            raise HTTPException(
                status_code=400,
                detail=f"External path is not writable: {target_folder.external_path}",
            )
        # Guard against path-traversal via a pathological filename — join then
        # verify the resolved destination is still inside the external dir.
        dest = (ext_dir / filename).resolve()
        try:
            dest.relative_to(ext_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid filename") from None
        if dest.exists():
            raise HTTPException(
                status_code=409,
                detail=f"A file named {filename!r} already exists in the external folder",
            )
        return dest, True
    ext = os.path.splitext(filename)[1].lower()
    return get_library_files_dir() / f"{uuid.uuid4().hex}{ext}", False


def _stored_file_path(abs_path: Path, is_external: bool) -> str:
    """Produce the value to persist in ``LibraryFile.file_path``.

    External files store the absolute mount path directly (same shape as scan
    produces), so ``to_absolute_path`` round-trips through its
    ``is_absolute()`` fast path. Managed files store a path relative to
    ``base_dir`` for portability.
    """
    return str(abs_path) if is_external else to_relative_path(abs_path)


async def save_3mf_bytes_to_library(
    db: AsyncSession,
    *,
    content: bytes,
    filename: str,
    folder: LibraryFolder | None = None,
    created_by_id: int | None = None,
    source_type: str | None = None,
    source_url: str | None = None,
    extra_metadata: dict | None = None,
    commit: bool = True,
) -> tuple[LibraryFile, bool]:
    """Persist raw 3MF / gcode bytes as a ``LibraryFile`` row.

    Used by automated sources that already hold the full file in memory —
    MakerWorld import (`source_type="makerworld"`, source_url=canonical URL)
    and slicer output (`source_type="sliced"`, source_url=NULL). Multipart
    uploads from the browser stay on the existing ``upload_file`` path,
    which has its own ``UploadFile``-specific plumbing.

    Returns ``(library_file, was_existing)``. When ``source_url`` is given
    and a non-trashed row already references that URL, returns the existing
    row immediately without rewriting bytes — that's the MakerWorld dedupe
    hot path. ``was_existing=True`` also fires when a different row shares
    the same content hash, so the caller can surface "already in library"
    UX even for plain re-uploads of the same plate.
    """

    # Source-URL dedupe: MakerWorld re-imports of the same plate must not
    # download + repack on every click. The route may also have done this
    # check itself; doing it here too keeps the helper safe to call from
    # paths that don't pre-check.
    if source_url:
        existing_by_url = (
            await db.execute(LibraryFile.active().where(LibraryFile.source_url == source_url).limit(1))
        ).scalar_one_or_none()
        if existing_by_url is not None:
            return existing_by_url, True

    ext = os.path.splitext(filename)[1].lower()
    file_type = detect_file_type(filename)

    file_path, is_external_upload = _resolve_upload_destination(folder, filename)
    with open(file_path, "wb") as f:
        f.write(content)

    file_hash = calculate_file_hash(file_path)

    metadata: dict = {}
    thumbnail_path: str | None = None
    thumbnails_dir = get_library_thumbnails_dir()

    if ext == ".3mf":
        try:
            parser = ThreeMFParser(str(file_path))
            raw_metadata = parser.parse()
            thumbnail_data = raw_metadata.get("_thumbnail_data")
            thumbnail_ext = raw_metadata.get("_thumbnail_ext", ".png")
            if thumbnail_data:
                thumb_filename = f"{uuid.uuid4().hex}{thumbnail_ext}"
                thumb_path = thumbnails_dir / thumb_filename
                with open(thumb_path, "wb") as f:
                    f.write(thumbnail_data)
                thumbnail_path = str(thumb_path)
            metadata = _clean_3mf_metadata(raw_metadata)
            try:
                import zipfile as _zf

                from backend.app.services.archive import parse_plates_from_3mf

                with _zf.ZipFile(str(file_path), "r") as _zfh:
                    plates_payload = parse_plates_from_3mf(_zfh)
                if plates_payload:
                    metadata["plates"] = plates_payload
                    metadata["is_multi_plate"] = len(plates_payload) > 1
            except Exception as _pe:
                logger.debug("Per-plate parse for save_3mf failed (non-critical): %s", _pe)
        except Exception as e:
            logger.warning("Failed to parse 3MF (save_3mf %s): %s", filename, e)

    elif ext == ".gcode":
        try:
            thumbnail_data = extract_gcode_thumbnail(file_path)
            if thumbnail_data:
                thumb_filename = f"{uuid.uuid4().hex}.png"
                thumb_path = thumbnails_dir / thumb_filename
                with open(thumb_path, "wb") as f:
                    f.write(thumbnail_data)
                thumbnail_path = str(thumb_path)
        except Exception as e:
            logger.warning("Failed to extract gcode thumbnail (save_3mf %s): %s", filename, e)

    if extra_metadata:
        metadata = {**metadata, **extra_metadata}

    fname_lower = filename.lower()
    swap_compatible = (
        fname_lower.endswith((".swap.3mf", ".swaps.3mf")) or ".swap." in fname_lower or ".swaps." in fname_lower
    )

    # Hash-based "already in library" hint — the caller may use it to render
    # an "exists already" badge. Independent of the source_url path above:
    # two different MakerWorld profiles can produce byte-identical 3MFs.
    dup_existing = (
        await db.execute(LibraryFile.active().where(LibraryFile.file_hash == file_hash).limit(1))
    ).scalar_one_or_none()
    was_existing = dup_existing is not None

    library_file = LibraryFile(
        folder_id=folder.id if folder is not None else None,
        is_external=is_external_upload,
        filename=filename,
        file_path=_stored_file_path(file_path, is_external_upload),
        file_type=file_type,
        file_tags=compute_file_tags(
            filename=filename,
            file_type=file_type,
            file_metadata=metadata or None,
            source_type=source_type,
            swap_compatible=swap_compatible,
        ),
        file_size=len(content),
        file_hash=file_hash,
        thumbnail_path=to_relative_path(thumbnail_path) if thumbnail_path else None,
        file_metadata=metadata or None,
        created_by_id=created_by_id,
        swap_compatible=swap_compatible,
        source_type=source_type,
        source_url=source_url,
    )
    db.add(library_file)
    await db.flush()
    # Inherit folder projects + plant matching plan rows. Caller is
    # responsible for ``selectinload(LibraryFolder.projects)`` on the
    # passed folder so this doesn't trip async lazy-load.
    await inherit_folder_projects(db, library_file, folder)
    if commit:
        await db.commit()
        await db.refresh(library_file)

    return library_file, was_existing


class _MoveSkip(Exception):
    """Signalled by ``_move_file_bytes`` to skip a file with a user-visible reason.

    Carries an optional ``code`` for machine-friendly grouping (the front-end
    can localise it) and a fallback English ``reason`` for logs.
    """

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _resolve_source_disk_path(file: LibraryFile) -> Path | None:
    """Return the absolute on-disk path for an existing LibraryFile, or None
    if it can't be located (legacy DB row, deleted file, etc.)."""
    if file.is_external:
        return Path(file.file_path) if file.file_path else None
    return to_absolute_path(file.file_path)


def _move_file_bytes(file: LibraryFile, target_folder: LibraryFolder | None) -> str:
    """Physically relocate ``file``'s bytes to match ``target_folder``.

    Used by the move endpoint when source/target straddle the
    managed↔external boundary (upstream #1112 follow-up — the prior
    implementation updated the DB row's ``folder_id`` but never moved the
    bytes, so a file moved to an external SMB folder showed up in the UI but
    not on the NAS).

    Returns the new ``file_path`` value to persist (relative for managed
    targets, absolute for external targets — matches the upload + scan paths).
    Raises ``_MoveSkip`` for any condition that would make the move unsafe
    (target unwritable, filename collision, source missing).

    Copy-then-unlink ordering: a partial copy followed by a failed unlink
    leaves both source and dest on disk — safer than the symmetric "rename or
    move" which would lose the source if the target write didn't complete on a
    flaky mount. The DB row stays pointed at the source until the caller
    commits the new ``file_path``.
    """
    src = _resolve_source_disk_path(file)
    if not src or not src.exists():
        raise _MoveSkip("source_missing", "source file missing on disk")

    target_is_external = target_folder is not None and target_folder.is_external

    if target_is_external:
        if target_folder.external_readonly:
            raise _MoveSkip("target_readonly", "target external folder is read-only")
        if not target_folder.external_path:
            raise _MoveSkip("target_misconfigured", "target external folder has no path")
        ext_dir = Path(target_folder.external_path)
        if not ext_dir.exists() or not ext_dir.is_dir():
            raise _MoveSkip("target_inaccessible", f"target path not accessible: {ext_dir}")
        if not os.access(ext_dir, os.W_OK):
            raise _MoveSkip("target_unwritable", f"target path not writable: {ext_dir}")
        dest = (ext_dir / file.filename).resolve()
        try:
            dest.relative_to(ext_dir.resolve())
        except ValueError:
            raise _MoveSkip("invalid_filename", f"unsafe filename: {file.filename!r}") from None
        if dest.exists():
            raise _MoveSkip("name_collision", f"a file named {file.filename!r} already exists in target")
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            with contextlib.suppress(OSError):
                dest.unlink(missing_ok=True)
            raise _MoveSkip("copy_failed", f"copy failed: {e}") from e
    else:
        # → managed (root or non-external folder): generate a fresh UUID
        # filename in the internal store so we don't collide with another file
        # that happens to share ``filename``.
        ext = src.suffix.lower()
        dest = get_library_files_dir() / f"{uuid.uuid4().hex}{ext}"
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            with contextlib.suppress(OSError):
                dest.unlink(missing_ok=True)
            raise _MoveSkip("copy_failed", f"copy failed: {e}") from e

    # Copy succeeded — unlink the original. Failure here leaves an orphan on
    # disk but the DB row is consistent against the new dest.
    try:
        src.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(
            "Move: copied %s -> %s but couldn't remove source: %s",
            src,
            dest,
            e,
        )

    return _stored_file_path(dest, is_external=target_is_external)


def extract_gcode_thumbnail(file_path: Path) -> bytes | None:
    """Extract embedded thumbnail from gcode file.

    Supports PrusaSlicer/BambuStudio format:
    ; thumbnail begin WxH SIZE
    ; base64data...
    ; thumbnail end
    """
    try:
        thumbnail_data = None
        in_thumbnail = False
        thumbnail_lines = []
        best_size = 0

        with open(file_path, errors="ignore") as f:
            # Only read first 50KB for performance (thumbnails are at the start)
            content = f.read(50000)

        for line in content.split("\n"):
            line = line.strip()

            # Check for thumbnail start
            if line.startswith("; thumbnail begin"):
                in_thumbnail = True
                thumbnail_lines = []
                # Parse dimensions: "; thumbnail begin 300x300 12345"
                match = re.search(r"(\d+)x(\d+)", line)
                if match:
                    width = int(match.group(1))
                    # Prefer larger thumbnails (up to 300px)
                    if width > best_size and width <= 300:
                        best_size = width
                continue

            # Check for thumbnail end
            if line.startswith("; thumbnail end"):
                if in_thumbnail and thumbnail_lines:
                    try:
                        # Decode the base64 data
                        b64_data = "".join(thumbnail_lines)
                        decoded = base64.b64decode(b64_data)
                        # Only keep if this is the best size or first valid thumbnail
                        if thumbnail_data is None or best_size > 0:
                            thumbnail_data = decoded
                    except (binascii.Error, ValueError):
                        pass  # Skip thumbnail with invalid base64 data
                in_thumbnail = False
                thumbnail_lines = []
                continue

            # Collect thumbnail data
            if in_thumbnail and line.startswith(";"):
                # Remove the leading "; " or ";"
                data_line = line[1:].strip()
                if data_line:
                    thumbnail_lines.append(data_line)

        return thumbnail_data
    except Exception as e:
        logger.warning("Failed to extract gcode thumbnail: %s", e)
        return None


def create_image_thumbnail(file_path: Path, thumbnails_dir: Path, max_size: int = 256) -> str | None:
    """Create a thumbnail from an image file.

    For small images, copies directly. For larger images, resizes.
    Returns the thumbnail path or None on failure.
    """
    try:
        from PIL import Image

        thumb_filename = f"{uuid.uuid4().hex}.png"
        thumb_path = thumbnails_dir / thumb_filename

        with Image.open(file_path) as img:
            # Convert to RGB if necessary (for PNG with transparency, etc.)
            if img.mode in ("RGBA", "LA", "P"):
                # Create white background for transparency
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # Resize if larger than max_size
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

            img.save(thumb_path, "PNG", optimize=True)

        return str(thumb_path)
    except ImportError:
        # PIL not installed, just copy the file if it's small enough
        logger.warning("PIL not installed, copying image as thumbnail")
        try:
            file_size = file_path.stat().st_size
            if file_size < 500000:  # Less than 500KB
                thumb_filename = f"{uuid.uuid4().hex}{file_path.suffix}"
                thumb_path = thumbnails_dir / thumb_filename
                shutil.copy2(file_path, thumb_path)
                return str(thumb_path)
        except OSError:
            pass  # File inaccessible; fall through to return None
        return None
    except Exception as e:
        logger.warning("Failed to create image thumbnail: %s", e)
        return None


# Supported image extensions for thumbnails
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}


# ============ Folder Endpoints ============


@router.get("/folders", response_model=list[FolderTreeItem])
@router.get("/folders/", response_model=list[FolderTreeItem])
async def list_folders(
    response: Response,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get all folders as a tree structure."""
    # Prevent browser caching of folder list
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"

    # m044: load projects via M2M selectinload (one extra IN-list query
    # rather than per-folder lazy fetch).
    result = await db.execute(
        select(LibraryFolder, PrintArchive.print_name)
        .outerjoin(PrintArchive, LibraryFolder.archive_id == PrintArchive.id)
        .options(selectinload(LibraryFolder.projects))
        .order_by(LibraryFolder.name)
    )
    rows = result.all()

    # Get file counts per folder
    file_counts_result = await db.execute(
        select(LibraryFile.folder_id, func.count(LibraryFile.id))
        .where(LibraryFile.folder_id.isnot(None))
        .group_by(LibraryFile.folder_id)
    )
    file_counts = dict(file_counts_result.all())

    # Build tree structure
    folder_map = {}
    root_folders = []

    for folder, archive_name in rows:
        folder_item = FolderTreeItem(
            id=folder.id,
            name=folder.name,
            parent_id=folder.parent_id,
            projects=_project_refs(folder.projects),
            archive_id=folder.archive_id,
            archive_name=archive_name,
            is_external=folder.is_external,
            external_path=folder.external_path,
            external_readonly=folder.external_readonly,
            file_count=file_counts.get(folder.id, 0),
            children=[],
        )
        folder_map[folder.id] = folder_item

    # Link children to parents
    for folder, _ in rows:
        folder_item = folder_map[folder.id]
        if folder.parent_id is None:
            root_folders.append(folder_item)
        elif folder.parent_id in folder_map:
            folder_map[folder.parent_id].children.append(folder_item)

    return root_folders


@router.get("/folders/by-project/{project_id}", response_model=list[FolderResponse])
async def get_folders_by_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get all folders linked to a specific project (via the M2M pivot)."""
    result = await db.execute(
        select(LibraryFolder)
        .join(library_folder_projects, library_folder_projects.c.folder_id == LibraryFolder.id)
        .where(library_folder_projects.c.project_id == project_id)
        .options(selectinload(LibraryFolder.projects))
        .order_by(LibraryFolder.name)
    )
    folders_orm = result.scalars().unique().all()

    folders = []
    for folder in folders_orm:
        file_count_result = await db.execute(
            select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == folder.id)
        )
        file_count = file_count_result.scalar() or 0

        folders.append(
            FolderResponse(
                id=folder.id,
                name=folder.name,
                parent_id=folder.parent_id,
                projects=_project_refs(folder.projects),
                archive_id=folder.archive_id,
                archive_name=None,
                is_external=folder.is_external,
                external_path=folder.external_path,
                external_readonly=folder.external_readonly,
                external_show_hidden=folder.external_show_hidden,
                file_count=file_count,
                created_at=folder.created_at,
                updated_at=folder.updated_at,
            )
        )

    return folders


@router.get("/folders/by-archive/{archive_id}", response_model=list[FolderResponse])
async def get_folders_by_archive(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get all folders linked to a specific archive."""
    result = await db.execute(
        select(LibraryFolder, PrintArchive.print_name)
        .outerjoin(PrintArchive, LibraryFolder.archive_id == PrintArchive.id)
        .where(LibraryFolder.archive_id == archive_id)
        .options(selectinload(LibraryFolder.projects))
        .order_by(LibraryFolder.name)
    )
    rows = result.all()

    folders = []
    for folder, archive_name in rows:
        # Get file count
        file_count_result = await db.execute(
            select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == folder.id)
        )
        file_count = file_count_result.scalar() or 0

        folders.append(
            FolderResponse(
                id=folder.id,
                name=folder.name,
                parent_id=folder.parent_id,
                projects=_project_refs(folder.projects),
                archive_id=folder.archive_id,
                archive_name=archive_name,
                is_external=folder.is_external,
                external_path=folder.external_path,
                external_readonly=folder.external_readonly,
                external_show_hidden=folder.external_show_hidden,
                file_count=file_count,
                created_at=folder.created_at,
                updated_at=folder.updated_at,
            )
        )

    return folders


@router.post("/folders", response_model=FolderResponse)
@router.post("/folders/", response_model=FolderResponse)
async def create_folder(
    data: FolderCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
):
    """Create a new folder."""
    # Verify parent exists if specified
    if data.parent_id is not None:
        parent_result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == data.parent_id))
        if not parent_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Parent folder not found")

    # m044: validate every requested project exists in one IN-list query.
    project_rows = await _resolve_projects_for_assign(db, data.project_ids)

    # Verify archive exists if specified
    archive_name = None
    if data.archive_id is not None:
        archive_result = await db.execute(select(PrintArchive).where(PrintArchive.id == data.archive_id))
        archive = archive_result.scalar_one_or_none()
        if not archive:
            raise HTTPException(status_code=404, detail="Archive not found")
        archive_name = archive.print_name

    folder = LibraryFolder(
        name=data.name,
        parent_id=data.parent_id,
        archive_id=data.archive_id,
    )
    folder.projects = project_rows
    db.add(folder)
    await db.commit()
    # Avoid db.refresh on the M2M relationship — async refresh of a
    # relationship attribute trips MissingGreenlet under FastAPI's
    # request loop. We've set ``folder.projects`` explicitly above and
    # the session is configured with ``expire_on_commit=False``, so the
    # in-session list is the authoritative final state.

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        projects=_project_refs(folder.projects),
        archive_id=folder.archive_id,
        archive_name=archive_name,
        is_external=folder.is_external,
        external_path=folder.external_path,
        external_readonly=folder.external_readonly,
        external_show_hidden=folder.external_show_hidden,
        file_count=0,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


@router.get("/folders/{folder_id}", response_model=FolderResponse)
async def get_folder(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get a folder by ID."""
    result = await db.execute(
        select(LibraryFolder, PrintArchive.print_name)
        .outerjoin(PrintArchive, LibraryFolder.archive_id == PrintArchive.id)
        .options(selectinload(LibraryFolder.projects))
        .where(LibraryFolder.id == folder_id)
    )
    row = result.one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder, archive_name = row

    # Get file count
    file_count_result = await db.execute(select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == folder_id))
    file_count = file_count_result.scalar() or 0

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        projects=_project_refs(folder.projects),
        archive_id=folder.archive_id,
        archive_name=archive_name,
        is_external=folder.is_external,
        external_path=folder.external_path,
        external_readonly=folder.external_readonly,
        external_show_hidden=folder.external_show_hidden,
        file_count=file_count,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


@router.put("/folders/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: int,
    data: FolderUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_UPDATE_ALL)),
):
    """Update a folder.

    Note: Folders require library:update_all permission since they don't have
    ownership tracking.
    """
    # m044: eager-load projects up front so the response build at the end
    # doesn't trigger a lazy fetch outside the async context.
    result = await db.execute(
        select(LibraryFolder).options(selectinload(LibraryFolder.projects)).where(LibraryFolder.id == folder_id)
    )
    folder = result.scalar_one_or_none()

    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    if data.name is not None:
        folder.name = data.name

    if data.parent_id is not None:
        # Prevent circular reference
        if data.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="Folder cannot be its own parent")

        # Check for circular reference in ancestors
        if data.parent_id != 0:  # 0 means move to root
            current_id = data.parent_id
            while current_id is not None:
                if current_id == folder_id:
                    raise HTTPException(status_code=400, detail="Cannot move folder into its own subtree")
                parent_result = await db.execute(select(LibraryFolder.parent_id).where(LibraryFolder.id == current_id))
                current_id = parent_result.scalar()

            folder.parent_id = data.parent_id
        else:
            folder.parent_id = None

    # m044: replace the folder's project list AND cascade the new list
    # to every child file so that linking a folder to a project backfills
    # the file→project pivot for each contained file (matches the legacy
    # "folder project inherits down to files" behaviour, generalised to
    # multi-project).
    if data.project_ids is not None:
        new_project_rows = await _resolve_projects_for_assign(db, data.project_ids)
        folder.projects = new_project_rows
        new_project_ids = [p.id for p in new_project_rows]

        # Mirror onto every child file's project list (replace semantics).
        child_files = (
            (
                await db.execute(
                    select(LibraryFile)
                    .where(LibraryFile.folder_id == folder_id)
                    .options(selectinload(LibraryFile.projects))
                )
            )
            .scalars()
            .all()
        )
        for child in child_files:
            child.projects = list(new_project_rows)

        # Reconcile print-plan rows for this folder's files with the new
        # project list (one plan row per (project, file) pair).
        await sync_plan_for_folder(db, folder_id=folder_id, project_ids=new_project_ids)

    # Update archive_id (0 to unlink)
    if data.archive_id is not None:
        if data.archive_id == 0:
            folder.archive_id = None
        else:
            # Verify archive exists
            archive_result = await db.execute(select(PrintArchive).where(PrintArchive.id == data.archive_id))
            if not archive_result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="Archive not found")
            folder.archive_id = data.archive_id

    await db.commit()

    # Re-fetch with selectinload after commit. Even with
    # ``expire_on_commit=False``, accessing ``server_default`` columns
    # like ``updated_at`` after a SQLAlchemy-tracked UPDATE expires
    # those attributes via ``populate_existing``, which then tries to
    # lazy-load outside the greenlet under FastAPI's request loop —
    # MissingGreenlet. A fresh select with eager projects avoids the
    # stale-attribute trap entirely.
    refreshed = (
        await db.execute(
            select(LibraryFolder).options(selectinload(LibraryFolder.projects)).where(LibraryFolder.id == folder_id)
        )
    ).scalar_one()

    file_count_result = await db.execute(select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == folder_id))
    file_count = file_count_result.scalar() or 0

    archive_name = None
    if refreshed.archive_id:
        archive_result = await db.execute(
            select(PrintArchive.print_name).where(PrintArchive.id == refreshed.archive_id)
        )
        archive_name = archive_result.scalar()

    return FolderResponse(
        id=refreshed.id,
        name=refreshed.name,
        parent_id=refreshed.parent_id,
        projects=_project_refs(refreshed.projects),
        archive_id=refreshed.archive_id,
        archive_name=archive_name,
        is_external=refreshed.is_external,
        external_path=refreshed.external_path,
        external_readonly=refreshed.external_readonly,
        external_show_hidden=refreshed.external_show_hidden,
        file_count=file_count,
        created_at=refreshed.created_at,
        updated_at=refreshed.updated_at,
    )


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_DELETE_ALL)),
):
    """Delete a folder and all its contents (cascade).

    Note: Folders require library:delete_all permission since they don't have
    ownership tracking.
    """
    result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))
    folder = result.scalar_one_or_none()

    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    # External folders: only remove DB records, never delete files from external path
    is_ext = folder.is_external

    # Get all files in this folder and subfolders to delete from disk
    async def get_all_file_ids(fid: int) -> list[int]:
        """Recursively get all file IDs in a folder tree."""
        file_ids = []

        # Get files in this folder
        files_result = await db.execute(
            select(LibraryFile.id, LibraryFile.file_path, LibraryFile.thumbnail_path, LibraryFile.is_external).where(
                LibraryFile.folder_id == fid
            )
        )
        for fid_val, file_path, thumb_path, file_is_ext in files_result.all():
            file_ids.append(fid_val)
            # Only delete non-external files from disk
            if not is_ext and not file_is_ext:
                try:
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                    if thumb_path and os.path.exists(thumb_path):
                        os.remove(thumb_path)
                except OSError as e:
                    logger.warning("Failed to delete file: %s", e)

        # Get child folders and recurse
        children_result = await db.execute(select(LibraryFolder.id).where(LibraryFolder.parent_id == fid))
        for (child_id,) in children_result.all():
            file_ids.extend(await get_all_file_ids(child_id))

        return file_ids

    await get_all_file_ids(folder_id)

    # Delete folder (cascade will handle files and subfolders)
    await db.delete(folder)
    await db.commit()

    return {"status": "success", "message": "Folder deleted"}


# ============ M2M project unlink (m044) ============

# These endpoints exist purely to drop a single (folder/file, project)
# pivot row without read-modify-write on the whole project list. Used by
# the project detail page's "remove from this project" affordance.


@router.delete("/folders/{folder_id}/projects/{project_id}", status_code=204)
async def unlink_folder_from_project(
    folder_id: int,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_UPDATE_ALL)),
):
    """Remove the (folder, project) pivot row. Idempotent: 404 only when
    the folder doesn't exist; missing pivot is treated as already-gone."""
    result = await db.execute(
        select(LibraryFolder).options(selectinload(LibraryFolder.projects)).where(LibraryFolder.id == folder_id)
    )
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    new_projects = [p for p in folder.projects if p.id != project_id]
    if len(new_projects) == len(folder.projects):
        return  # Idempotent: already not linked.

    folder.projects = new_projects
    new_project_ids = [p.id for p in new_projects]
    # Cascade onto child files (same replace semantics the folder PUT uses).
    child_files = (
        (
            await db.execute(
                select(LibraryFile)
                .where(LibraryFile.folder_id == folder_id)
                .options(selectinload(LibraryFile.projects))
            )
        )
        .scalars()
        .all()
    )
    for child in child_files:
        child.projects = list(new_projects)
    await sync_plan_for_folder(db, folder_id=folder_id, project_ids=new_project_ids)
    await db.commit()


@router.delete("/files/{file_id}/projects/{project_id}", status_code=204)
async def unlink_file_from_project(
    file_id: int,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_UPDATE_ALL,
            Permission.LIBRARY_UPDATE_OWN,
        )
    ),
):
    """Remove the (file, project) pivot row. Idempotent: missing pivot
    treated as already-gone."""
    user, can_modify_all = auth_result

    result = await db.execute(
        select(LibraryFile).options(selectinload(LibraryFile.projects)).where(LibraryFile.id == file_id)
    )
    file = result.scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    if not can_modify_all and file.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="You can only update your own files")

    new_projects = [p for p in file.projects if p.id != project_id]
    if len(new_projects) == len(file.projects):
        return  # Idempotent.

    file.projects = new_projects
    await sync_plan_for_file(
        db,
        library_file_id=file.id,
        project_ids=[p.id for p in new_projects],
        file_type=file.file_type,
    )
    await db.commit()


# ============ External Folder Endpoints ============

# Blocked system directories that cannot be mounted
_BLOCKED_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/boot",
    "/sbin",
    "/bin",
    "/usr/sbin",
    "/usr/bin",
    "/lib",
    "/etc",
)

# Supported file extensions for external folder scanning
_SCANNABLE_EXTENSIONS = {
    ".3mf",
    ".gcode",
    ".gcode.3mf",
    ".stl",
    ".obj",
    ".step",
    ".stp",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
}


def _validate_external_path(path_str: str) -> Path:
    """Validate an external path is safe to mount."""
    path = Path(path_str).resolve()

    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="Path must be absolute")

    for prefix in _BLOCKED_PREFIXES:
        if str(path).startswith(prefix):
            raise HTTPException(status_code=400, detail=f"Cannot mount system directory: {prefix}")

    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Path does not exist: {path}")

    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {path}")

    # Check readability
    if not os.access(path, os.R_OK):
        raise HTTPException(status_code=400, detail=f"Path is not readable: {path}")

    return path


@router.post("/folders/external", response_model=FolderResponse)
async def create_external_folder(
    data: ExternalFolderCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
):
    """Create an external folder that points to a host directory."""
    resolved = _validate_external_path(data.external_path)

    # Check no other external folder already points to this path
    existing = await db.execute(
        select(LibraryFolder).where(
            LibraryFolder.is_external.is_(True),
            LibraryFolder.external_path == str(resolved),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An external folder already exists for this path")

    # Verify parent exists if specified
    if data.parent_id is not None:
        parent_result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == data.parent_id))
        if not parent_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Parent folder not found")

    folder = LibraryFolder(
        name=data.name,
        parent_id=data.parent_id,
        is_external=True,
        external_path=str(resolved),
        external_readonly=data.readonly,
        external_show_hidden=data.show_hidden,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        projects=[],
        archive_id=None,
        is_external=True,
        external_path=folder.external_path,
        external_readonly=folder.external_readonly,
        external_show_hidden=folder.external_show_hidden,
        file_count=0,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


@router.post("/folders/{folder_id}/scan")
async def scan_external_folder(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
):
    """Scan an external folder and sync files to the database.

    Discovers new files, removes DB entries for deleted files.
    Does not copy files - stores the external path directly.
    """
    result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))
    folder = result.scalar_one_or_none()

    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    if not folder.is_external or not folder.external_path:
        raise HTTPException(status_code=400, detail="Not an external folder")

    ext_path = Path(folder.external_path)
    if not ext_path.exists() or not ext_path.is_dir():
        raise HTTPException(status_code=400, detail=f"External path is not accessible: {folder.external_path}")

    # Collect all existing child external subfolder IDs (walk parent chain to find descendants)
    all_folder_ids = {folder_id}
    queue = [folder_id]
    while queue:
        parent = queue.pop()
        children_result = await db.execute(select(LibraryFolder.id).where(LibraryFolder.parent_id == parent))
        for (child_id,) in children_result.all():
            all_folder_ids.add(child_id)
            queue.append(child_id)

    # Get existing DB files across ALL folder IDs (root + subfolders)
    existing_result = await db.execute(
        select(LibraryFile).where(LibraryFile.folder_id.in_(all_folder_ids), LibraryFile.is_external.is_(True))
    )
    existing_files = {f.file_path: f for f in existing_result.scalars().all()}

    # Build folder cache mapping relative paths to folder IDs
    folder_cache: dict[str, int] = {"": folder_id}

    # Scan the directory
    added = 0
    removed = 0
    found_paths = set()

    for dirpath, dirnames, filenames in os.walk(ext_path):
        # Filter hidden directories unless configured
        if not folder.external_show_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        # Compute relative directory path from ext_path
        rel_dir = str(Path(dirpath).relative_to(ext_path)).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""

        # Create subfolder chain in DB when new directories are encountered
        target_folder_id = folder_cache.get(rel_dir)
        if target_folder_id is None:
            parts = rel_dir.split("/")
            current_path = ""
            current_parent_id = folder_id
            for part in parts:
                current_path = f"{current_path}/{part}" if current_path else part
                if current_path in folder_cache:
                    current_parent_id = folder_cache[current_path]
                else:
                    # Create subfolder in DB
                    new_folder = LibraryFolder(
                        name=part,
                        parent_id=current_parent_id,
                        is_external=True,
                        external_path=str(ext_path / current_path),
                        external_show_hidden=folder.external_show_hidden,
                    )
                    db.add(new_folder)
                    await db.flush()
                    folder_cache[current_path] = new_folder.id
                    all_folder_ids.add(new_folder.id)
                    current_parent_id = new_folder.id
            target_folder_id = folder_cache[current_path]
        for filename in filenames:
            # Skip hidden files unless configured
            if not folder.external_show_hidden and filename.startswith("."):
                continue

            filepath = Path(dirpath) / filename
            ext = filepath.suffix.lower()

            # Check for compound extensions like .gcode.3mf
            if ext not in _SCANNABLE_EXTENSIONS:
                # Check compound
                compound = "".join(filepath.suffixes[-2:]).lower() if len(filepath.suffixes) >= 2 else ""
                if compound not in _SCANNABLE_EXTENSIONS:
                    continue

            # Resolve symlinks and ensure still under external_path
            try:
                real_path = filepath.resolve()
                real_path.relative_to(ext_path.resolve())
            except (ValueError, OSError):
                continue  # Symlink escapes the external dir

            file_path_str = str(filepath)
            found_paths.add(file_path_str)

            if file_path_str in existing_files:
                continue  # Already tracked

            # Get file info
            try:
                stat = filepath.stat()
            except OSError:
                continue

            file_type = detect_file_type(filepath.name)
            # Sliced 3MFs (`.gcode.3mf`) collapse to file_type='gcode' but
            # still need the 3MF parser path for thumbnail + plate cache.
            # Branch by container suffix, not primary type.
            is_3mf_container = filepath.name.lower().endswith(".3mf")

            # Extract thumbnail for 3mf files
            thumbnail_path = None
            file_metadata = None
            if is_3mf_container:
                try:
                    parser = ThreeMFParser(str(filepath))
                    meta = parser.parse()
                    if meta:
                        file_metadata = _clean_3mf_metadata(meta)
                    thumb_data = parser.extract_thumbnail()
                    if thumb_data:
                        thumb_dir = get_library_thumbnails_dir()
                        thumb_filename = f"{uuid.uuid4().hex}.png"
                        thumb_full = thumb_dir / thumb_filename
                        thumb_full.write_bytes(thumb_data)
                        thumbnail_path = to_relative_path(thumb_full)
                    # Same per-plate cache populated as the upload route —
                    # external 3MFs imported via folder-scan benefit too.
                    try:
                        import zipfile as _zf

                        from backend.app.services.archive import parse_plates_from_3mf

                        with _zf.ZipFile(str(filepath), "r") as _zfh:
                            plates_payload = parse_plates_from_3mf(_zfh)
                        if plates_payload and file_metadata is not None:
                            file_metadata["plates"] = plates_payload
                            file_metadata["is_multi_plate"] = len(plates_payload) > 1
                    except Exception as _pe:
                        logger.debug("Per-plate parse for external scan failed (non-critical): %s", _pe)
                except Exception as e:
                    logger.debug("Failed to extract metadata from external 3mf %s: %s", filepath, e)

            # Generate thumbnail for STL files
            if file_type == "stl" and thumbnail_path is None:
                try:
                    thumb_dir = get_library_thumbnails_dir()
                    thumb_result = generate_stl_thumbnail(str(filepath), str(thumb_dir))
                    if thumb_result:
                        thumbnail_path = to_relative_path(Path(thumb_result))
                except Exception as e:
                    logger.debug("Failed to generate STL thumbnail for external %s: %s", filepath, e)

            # Extract gcode thumbnail — only for raw .gcode files; sliced
            # .gcode.3mf already went through the 3MF parser branch above.
            if file_type == "gcode" and not is_3mf_container and thumbnail_path is None:
                thumb_data = extract_gcode_thumbnail(filepath)
                if thumb_data:
                    thumb_dir = get_library_thumbnails_dir()
                    thumb_filename = f"{uuid.uuid4().hex}.png"
                    thumb_full = thumb_dir / thumb_filename
                    thumb_full.write_bytes(thumb_data)
                    thumbnail_path = to_relative_path(thumb_full)

            # Create thumbnail for image files
            if ext.lower() in IMAGE_EXTENSIONS and thumbnail_path is None:
                thumbnail_path_str = create_image_thumbnail(filepath, get_library_thumbnails_dir())
                if thumbnail_path_str:
                    thumbnail_path = to_relative_path(Path(thumbnail_path_str))

            db_file = LibraryFile(
                folder_id=target_folder_id,
                is_external=True,
                filename=filename,
                file_path=file_path_str,
                file_type=file_type,
                file_tags=compute_file_tags(
                    filename=filename,
                    file_type=file_type,
                    file_metadata=file_metadata,
                    source_type=None,
                    swap_compatible=False,
                ),
                file_size=stat.st_size,
                file_hash=None,  # Skip hashing external files for performance
                thumbnail_path=thumbnail_path,
                file_metadata=file_metadata,
            )
            db.add(db_file)
            added += 1

    # Remove DB entries for files that no longer exist on disk
    for path_str, db_file in existing_files.items():
        if path_str not in found_paths:
            # Clean up thumbnail if we generated one
            if db_file.thumbnail_path:
                try:
                    abs_thumb = to_absolute_path(db_file.thumbnail_path)
                    if abs_thumb and abs_thumb.exists():
                        abs_thumb.unlink()
                except OSError:
                    pass
            await db.delete(db_file)
            removed += 1

    # Clean up orphaned subfolders (directories that no longer exist on disk)
    # Re-fetch all child external subfolders
    all_sub_result = await db.execute(
        select(LibraryFolder).where(
            LibraryFolder.id.in_(all_folder_ids),
            LibraryFolder.id != folder_id,
        )
    )
    all_subfolders = all_sub_result.scalars().all()

    # Process deepest-first (sort by path depth descending)
    all_subfolders.sort(key=lambda f: f.external_path.count("/") if f.external_path else 0, reverse=True)

    for sub in all_subfolders:
        if sub.external_path and not Path(sub.external_path).exists():
            # Delete only if folder has no files and no child folders remaining
            file_count = await db.execute(select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == sub.id))
            if (file_count.scalar() or 0) > 0:
                continue
            child_count = await db.execute(
                select(func.count(LibraryFolder.id)).where(LibraryFolder.parent_id == sub.id)
            )
            if (child_count.scalar() or 0) > 0:
                continue
            await db.delete(sub)

    await db.commit()

    return {"status": "success", "added": added, "removed": removed}


# ============ File Endpoints ============


@router.get("/files", response_model=list[FileListResponse])
@router.get("/files/", response_model=list[FileListResponse])
async def list_files(
    response: Response,
    folder_id: int | None = None,
    project_id: int | None = None,
    include_root: bool = True,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """List files, optionally filtered by folder or project.

    Args:
        folder_id: Filter by folder ID. If None and include_root=True, returns root files.
        project_id: Return all files across folders linked to this project (bulk fetch, avoids N+1).
        include_root: If True and folder_id is None, returns files at root level.
                     If False and folder_id is None, returns all files.
    """
    # Trash bin (#1008): exclude soft-deleted rows from the main listing.
    # Users manage trashed files via /library/trash endpoints instead.
    # m044: also eagerly load the M2M projects collection so each
    # FileListResponse can carry project_ids without an N+1.
    query = LibraryFile.active().options(
        selectinload(LibraryFile.created_by),
        selectinload(LibraryFile.projects),
    )

    if folder_id is not None:
        query = query.where(LibraryFile.folder_id == folder_id)
    elif project_id is not None:
        # m044: a file participates in a project either via the direct
        # file→project pivot OR via the folder→project pivot of its
        # containing folder. Union the two so the project detail page
        # surfaces both groups in one query.
        direct_files = select(library_file_projects.c.file_id).where(library_file_projects.c.project_id == project_id)
        inherited_files = (
            select(LibraryFile.id)
            .join(LibraryFolder, LibraryFile.folder_id == LibraryFolder.id)
            .join(library_folder_projects, library_folder_projects.c.folder_id == LibraryFolder.id)
            .where(library_folder_projects.c.project_id == project_id)
        )
        query = query.where(LibraryFile.id.in_(direct_files.union(inherited_files)))
    elif include_root:
        query = query.where(LibraryFile.folder_id.is_(None))

    query = query.order_by(LibraryFile.filename)
    result = await db.execute(query)
    files = result.scalars().all()

    # Get duplicate counts
    hash_counts = {}
    if files:
        hashes = [f.file_hash for f in files if f.file_hash]
        if hashes:
            # Trashed files are excluded — they are not a "source of truth" for
            # dedup; a duplicate badge against a trashed sibling is misleading.
            dup_result = await db.execute(
                select(LibraryFile.file_hash, func.count(LibraryFile.id))
                .where(LibraryFile.file_hash.in_(hashes), LibraryFile.deleted_at.is_(None))
                .group_by(LibraryFile.file_hash)
            )
            hash_counts = {h: c - 1 for h, c in dup_result.all()}  # -1 to exclude self

    # Notes counts (gh#3) - single grouped query, no N+1.
    notes_counts: dict[int, int] = {}
    if files:
        from backend.app.models.library_file_note import LibraryFileNote

        note_result = await db.execute(
            select(LibraryFileNote.library_file_id, func.count(LibraryFileNote.id))
            .where(LibraryFileNote.library_file_id.in_([f.id for f in files]))
            .group_by(LibraryFileNote.library_file_id)
        )
        notes_counts = dict(note_result.all())

    # Prevent browser caching of file list
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"

    file_list = []
    for f in files:
        # Extract key metadata for display
        print_name = None
        print_time = None
        filament_grams = None
        sliced_for_model = None
        object_count = None
        is_multi_plate = False
        if f.file_metadata:
            print_name = f.file_metadata.get("print_name")
            print_time = f.file_metadata.get("print_time_seconds")
            filament_grams = f.file_metadata.get("filament_used_grams")
            sliced_for_model = f.file_metadata.get("sliced_for_model")
            printable_objects = f.file_metadata.get("printable_objects")
            if isinstance(printable_objects, dict):
                object_count = len(printable_objects)
            # ``is_multi_plate`` is pre-computed at upload + by m023 backfill
            # so the frontend can gate gallery rendering without an extra
            # /plates fetch per single-plate file.
            is_multi_plate = bool(f.file_metadata.get("is_multi_plate"))

            # Multi-plate files: replace the single-plate snapshot values
            # with sums across every plate. The card represents the WHOLE
            # file, so showing only plate 1's time / weight / object count
            # is misleading. The cached ``plates`` array (m023) carries
            # everything we need — no ZIP open.
            if is_multi_plate:
                plates_payload = f.file_metadata.get("plates")
                if isinstance(plates_payload, list) and plates_payload:
                    time_sum = 0
                    grams_sum = 0.0
                    objects_sum = 0
                    for p in plates_payload:
                        pt = p.get("print_time_seconds") if isinstance(p, dict) else None
                        if isinstance(pt, (int, float)):
                            time_sum += int(pt)
                        pg = p.get("filament_used_grams") if isinstance(p, dict) else None
                        if isinstance(pg, (int, float)):
                            grams_sum += float(pg)
                        po = p.get("printable_objects") if isinstance(p, dict) else None
                        if isinstance(po, dict):
                            objects_sum += len(po)
                    if time_sum > 0:
                        print_time = time_sum
                    if grams_sum > 0:
                        filament_grams = round(grams_sum, 1)
                    if objects_sum > 0:
                        object_count = objects_sum

        file_list.append(
            FileListResponse(
                id=f.id,
                folder_id=f.folder_id,
                project_ids=[p.id for p in f.projects],
                is_external=f.is_external,
                filename=f.filename,
                file_type=f.file_type,
                file_tags=f.file_tags or [],
                file_size=f.file_size,
                thumbnail_path=f.thumbnail_path,
                duplicate_count=hash_counts.get(f.file_hash, 0) if f.file_hash else 0,
                created_by_id=f.created_by_id,
                created_by_username=f.created_by.username if f.created_by else None,
                created_at=f.created_at,
                print_name=print_name,
                print_time_seconds=print_time,
                filament_used_grams=filament_grams,
                object_count=object_count,
                sliced_for_model=sliced_for_model,
                swap_compatible=f.swap_compatible,
                is_multi_plate=is_multi_plate,
                source_type=f.source_type,
                source_url=f.source_url,
                notes_count=notes_counts.get(f.id, 0),
            )
        )

    return file_list


# =====================================================================
# Server-side slicing (B.4 — Phase 1.D of 0.5.x cycle)
# =====================================================================
# Three helpers + the library-side slice route. The archive-side slice
# route lives in archives.py and reuses ``slice_and_persist_as_archive``
# below. Order: helpers first because ``slice_library_file`` and the
# archive route both call into them.


# Keys in ``Metadata/project_settings.config`` that BambuStudio writes ``"-1"``
# to when the user wants the value inherited from the parent process preset.
# The CLI's ``StaticPrintConfig`` validator runs against the embedded settings
# *before* ``--load-settings`` overrides apply, so a sentinel ``"-1"`` trips
# the field's lower-bound range check and the CLI exits non-zero before our
# profile triplet is ever consulted (upstream Bambuddy #1201 — MakerWorld P2S
# models).
#
# Allowlisted (rather than "strip every '-1' value") because some fields
# legitimately accept negative numbers (z_offset, translation values, etc.)
# and a blanket strip would silently corrupt those.
#
# Add new entries here as more reports surface — the slicer's error message
# names the offending field directly (``<field>: -1 not in range [...]``).
_PROJECT_SETTINGS_SENTINEL_KEYS = frozenset(
    {
        # Reported in upstream #1201 (MakerWorld P2S 3MFs).
        "raft_first_layer_expansion",
        "tree_support_wall_count",
        # Cited in the strip-experiment comment block inside
        # ``_run_slicer_with_fallback`` as a known sentinel case from earlier
        # reports.
        "prime_tower_brim_width",
    }
)


def _sanitize_project_settings_sentinels(zip_bytes: bytes) -> bytes:
    """Strip ``"-1"`` inherit-from-parent sentinels from the 3MF's
    ``Metadata/project_settings.config`` so the slicer CLI's range validator
    accepts the file (upstream #1201).

    Removes only allowlisted keys (see ``_PROJECT_SETTINGS_SENTINEL_KEYS``)
    when their value is exactly ``"-1"``. The rest of the config — and every
    other entry in the zip — is preserved byte-for-byte. Unlike a
    full-strip-every-config approach (cautioned against in the comment block
    inside ``_run_slicer_with_fallback``) this leaves ``StaticPrintConfig``
    initialisation intact: the file is still present, still parses, and the
    slicer falls back to the supplied ``--load-settings`` value for the
    removed key.

    Returns the original bytes unchanged when no sanitisation is needed
    (input isn't a valid zip, no ``project_settings.config``, no allowlisted
    sentinels present, malformed JSON, non-dict root, or any other parse
    failure) so the caller can pass the result on without further checks.
    """
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(zip_bytes), "r") as zin:
            if "Metadata/project_settings.config" not in zin.namelist():
                return zip_bytes
            try:
                config = json.loads(zin.read("Metadata/project_settings.config").decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return zip_bytes
            if not isinstance(config, dict):
                return zip_bytes
            removed = [key for key in _PROJECT_SETTINGS_SENTINEL_KEYS if config.get(key) == "-1"]
            if not removed:
                return zip_bytes
            for key in removed:
                config.pop(key, None)
            patched = json.dumps(config)
            logger.info(
                "3MF sanitiser: removed sentinel '-1' for keys %s — slicer will use --load-settings defaults",
                sorted(removed),
            )
            dst = BytesIO()
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename == "Metadata/project_settings.config":
                        zout.writestr(item, patched)
                    else:
                        zout.writestr(item, zin.read(item.filename))
            return dst.getvalue()
    except (zipfile.BadZipFile, OSError):
        return zip_bytes


async def _run_slicer_with_fallback(
    db: AsyncSession,
    *,
    model_bytes: bytes,
    model_filename: str,
    request,  # SliceRequest — typed loosely so the import doesn't shadow upload's local
    current_user_id: int | None = None,
    job_id: int | None = None,
):
    """Validate presets, dispatch to the right sidecar, run the slicer with
    the auto-fallback for 3MF inputs whose ``--load-settings`` path crashes
    the CLI. Returns ``(SliceResult, used_embedded_settings: bool)``. Raises
    :class:`HTTPException` for any caller-facing error.

    ``current_user_id`` is needed to resolve **cloud** presets — the cloud
    token is per-user. For the legacy / local-only path it can be left
    ``None``.

    ``job_id``: when set, a request_id is generated and a parallel poller
    pushes the sidecar's --pipe-fed progress events onto
    :meth:`SliceDispatchService.set_progress` so the UI's persistent toast
    can show "Generating G-code (75%)" instead of just elapsed time. Pass
    ``None`` for synchronous routes that aren't tracked by the dispatcher.
    """
    from backend.app.services.preset_resolver import resolve_preset_ref
    from backend.app.services.slicer_api import (
        SlicerApiServerError,
        SlicerApiService,
        SlicerApiUnavailableError,
        SlicerInputError,
    )
    from backend.app.services.slicer_routing import resolve_sidecar_url, slicer_label

    user: User | None = None
    if current_user_id is not None:
        user = await db.get(User, current_user_id)

    presets: dict[str, str] = {}
    refs = {
        "printer": request.printer_preset,
        "process": request.process_preset,
    }
    for slot, ref in refs.items():
        assert ref is not None, "schema validator guarantees PresetRef is set"
        presets[slot] = await resolve_preset_ref(db, user, ref, slot)
    # Multi-color: resolve each filament slot in plate order. The schema
    # validator backfills ``filament_presets`` from the legacy singular
    # field for older single-color callers, so this list is non-empty.
    filament_jsons: list[str] = []
    for ref in request.filament_presets:
        assert ref is not None, "schema validator guarantees filament list is non-None"
        filament_jsons.append(await resolve_preset_ref(db, user, ref, "filament"))

    # Slicer routing — per-request override on the SliceRequest wins over
    # the global preferred_slicer setting; per-install URL setting wins
    # over the SLICER_API_URL / BAMBU_STUDIO_API_URL env defaults from
    # core/config.py.
    chosen, api_url = await resolve_sidecar_url(db, slicer_override=request.slicer)
    if chosen is None:
        raise HTTPException(
            status_code=400,
            detail="Unknown preferred_slicer setting. Expected 'orcaslicer' or 'bambu_studio'.",
        )
    if not api_url:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{slicer_label(chosen)} API URL is empty -- configure it in Settings → "
                "Profiles → Slicer API, or pick a different slicer in the Slice modal."
            ),
        )

    # Forward original 3MF bytes — stripping Metadata/project_settings.config
    # / model_settings.config / slice_info.config / cut_information.xml looks
    # tempting (the theory being --load-settings would then take precedence
    # cleanly) but breaks the CLI: model_settings.config carries the plate
    # definitions the CLI needs to map ``--slice N`` to a real plate, and
    # slice_info / project_settings supply baseline config the CLI's
    # StaticPrintConfig pass needs at all. Stripping any of them caused the
    # CLI to silently exit immediately after "Initializing
    # StaticPrintConfigs" — exit code 0, no result.json, no stderr — which
    # Node's child_process treated as failure and the slice service then
    # masked by falling back to slice_without_profiles using the un-stripped
    # bytes (and the source's embedded printer). Net effect: every 3MF slice
    # with profiles silently produced wrong-printer output. Forwarding the
    # original bytes lets --load-settings override the specific fields the
    # user changed (printer/process/filament) while the embedded plate /
    # model definitions remain intact.
    is_3mf = model_filename.lower().endswith(".3mf")
    primary_bytes = model_bytes
    if is_3mf:
        # Strip "-1" inherit-from-parent sentinels from
        # Metadata/project_settings.config so the CLI's StaticPrintConfig
        # range validator accepts the file (upstream #1201). Surgical —
        # keeps the config present, just removes the offending keys; the
        # supplied --load-settings (and the fallback's embedded values for
        # keys we didn't touch) still drive the slice.
        primary_bytes = _sanitize_project_settings_sentinels(primary_bytes)

    used_embedded_settings = False
    service = SlicerApiService(api_url)
    progress_request_id: str | None = None
    progress_callback = None
    if job_id is not None:
        from uuid import uuid4

        from backend.app.services.slice_dispatch import slice_dispatch as _dispatch

        progress_request_id = str(uuid4())

        def _on_progress(snapshot: dict) -> None:
            _dispatch.set_progress(job_id, snapshot)

        progress_callback = _on_progress
    try:
        try:
            result = await service.slice_with_profiles(
                model_bytes=primary_bytes,
                model_filename=model_filename,
                printer_profile_json=presets["printer"],
                process_profile_json=presets["process"],
                filament_profile_jsons=filament_jsons,
                plate=request.plate,
                export_3mf=request.export_3mf,
                bed_type=request.bed_type,
                request_id=progress_request_id,
                on_progress=progress_callback,
            )
        except SlicerApiServerError as exc:
            if not is_3mf:
                raise
            logger.warning(
                "Slicer CLI rejected --load-settings for %s (%s); retrying with embedded settings",
                model_filename,
                exc,
            )
            # Forward the same request_id + callback so the toast's live
            # progress keeps updating across the fallback retry instead of
            # going blank for the rest of the slice. Use the sanitised
            # bytes — the embedded-settings path also reads the same
            # project_settings.config and the same range validator runs
            # there too, so without sanitisation the fallback would die
            # on the same sentinel error (#1201).
            result = await service.slice_without_profiles(
                model_bytes=primary_bytes,
                model_filename=model_filename,
                plate=request.plate,
                export_3mf=request.export_3mf,
                bed_type=request.bed_type,
                request_id=progress_request_id,
                on_progress=progress_callback,
            )
            used_embedded_settings = True
    except SlicerInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SlicerApiServerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SlicerApiUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await service.close()

    return result, used_embedded_settings


async def slice_and_persist(
    db: AsyncSession,
    *,
    model_bytes: bytes,
    model_filename: str,
    folder_id: int | None,
    extra_metadata: dict | None,
    request,  # SliceRequest
    current_user_id: int | None,
    job_id: int | None = None,
):
    """Slice a model and save the result as a new ``LibraryFile`` in
    ``folder_id`` (same folder as the source by convention).

    Always exports as ``.gcode.3mf`` so the existing library thumbnail
    pipeline works on the new file. Plain ``.gcode`` would have no embedded
    thumbnail to extract.
    """
    from backend.app.schemas.slicer import SliceResponse

    library_request = request.model_copy(update={"export_3mf": True})

    result, used_embedded_settings = await _run_slicer_with_fallback(
        db,
        model_bytes=model_bytes,
        model_filename=model_filename,
        request=library_request,
        current_user_id=current_user_id,
        job_id=job_id,
    )

    base_name = model_filename.rsplit(".", 1)[0]
    out_filename = f"{base_name}.gcode.3mf"
    unique_name = f"{uuid.uuid4().hex}.gcode.3mf"
    out_path = get_library_files_dir() / unique_name
    out_path.write_bytes(result.content)

    # Extract thumbnail from the produced 3MF so the library card shows a
    # preview. Failures here aren't fatal — the file is still useful.
    thumbnail_relative: str | None = None
    parsed_metadata: dict = {}
    try:
        parser = ThreeMFParser(str(out_path))
        parsed = parser.parse()
        thumb_data = parsed.get("_thumbnail_data")
        thumb_ext = parsed.get("_thumbnail_ext", ".png")
        if thumb_data:
            thumb_filename = f"{uuid.uuid4().hex}{thumb_ext}"
            thumb_path = get_library_thumbnails_dir() / thumb_filename
            thumb_path.write_bytes(thumb_data)
            thumbnail_relative = to_relative_path(thumb_path)
        cleaned = _clean_3mf_metadata(parsed)
        if isinstance(cleaned, dict):
            parsed_metadata = cleaned
    except Exception as exc:
        logger.warning("Failed to parse sliced 3MF metadata for %s: %s", out_filename, exc)

    # The parsed 3MF metadata carries a ``print_name`` lifted from the source
    # file's embedded settings (BambuStudio always sets this; OrcaSlicer
    # often leaves it blank). The FileManager listing prefers print_name
    # over filename for display, which makes a sliced row indistinguishable
    # from its source. Drop print_name so the listing falls back to the
    # actual filename — which already ends in ".gcode.3mf" and self-describes
    # as the sliced output.
    metadata: dict = {k: v for k, v in parsed_metadata.items() if k != "print_name"}
    metadata.update(
        {
            "print_time_seconds": result.print_time_seconds,
            "filament_used_g": result.filament_used_g,
            "filament_used_mm": result.filament_used_mm,
        }
    )
    if used_embedded_settings:
        metadata["used_embedded_settings"] = True
    if extra_metadata:
        metadata.update(extra_metadata)

    new_file = LibraryFile(
        folder_id=folder_id,
        filename=out_filename,
        file_path=to_relative_path(out_path),
        # Sliced output is a ``.gcode.3mf`` zip with embedded G-code, but the
        # user-facing meaning is "ready-to-print G-code" — using ``"gcode"``
        # gives it the same badge as plain .gcode files and distinguishes it
        # from un-sliced ``.3mf`` source models. ``compute_file_tags`` adds
        # both ``gcode`` AND ``3mf`` tags so the composite badge restores
        # the visual distinction in the UI.
        file_type="gcode",
        file_tags=compute_file_tags(
            filename=out_filename,
            file_type="gcode",
            file_metadata=metadata,
            source_type="sliced",
            swap_compatible=False,
        ),
        file_size=len(result.content),
        file_hash=hashlib.sha256(result.content).hexdigest(),
        thumbnail_path=thumbnail_relative,
        file_metadata=metadata,
        source_type="sliced",
        created_by_id=current_user_id,
    )
    db.add(new_file)
    await db.flush()
    # Inherit target folder's projects + plant matching plan rows so a
    # sliced ``.gcode.3mf`` lands in the project's plan automatically.
    # ``slice_and_persist`` doesn't load the folder itself — fetch with
    # selectinload so the inherit helper doesn't trip async lazy-load.
    if folder_id is not None:
        target_folder_for_inherit = (
            await db.execute(
                select(LibraryFolder).where(LibraryFolder.id == folder_id).options(selectinload(LibraryFolder.projects))
            )
        ).scalar_one_or_none()
        if target_folder_for_inherit is not None:
            await inherit_folder_projects(db, new_file, target_folder_for_inherit)
    await db.commit()
    await db.refresh(new_file)

    return SliceResponse(
        library_file_id=new_file.id,
        name=new_file.filename,
        print_time_seconds=result.print_time_seconds,
        filament_used_g=result.filament_used_g,
        filament_used_mm=result.filament_used_mm,
        used_embedded_settings=used_embedded_settings,
    )


async def slice_and_persist_as_archive(
    db: AsyncSession,
    *,
    model_bytes: bytes,
    model_filename: str,
    request,  # SliceRequest
    source_archive,  # PrintArchive — hint kept loose to avoid cyclic import
    current_user_id: int | None,
    job_id: int | None = None,
):
    """Slice a model and save the result as a new ``PrintArchive`` row,
    inheriting printer / project / makerworld metadata from the source
    archive. Always exports as a ``.gcode.3mf`` so the existing thumbnail
    and plates infrastructure works on the new archive."""
    from backend.app.schemas.slicer import SliceArchiveResponse

    archive_request = request.model_copy(update={"export_3mf": True})

    result, used_embedded_settings = await _run_slicer_with_fallback(
        db,
        model_bytes=model_bytes,
        model_filename=model_filename,
        request=archive_request,
        job_id=job_id,
        current_user_id=current_user_id,
    )

    base_name = model_filename.rsplit(".", 1)[0]
    out_filename = f"{base_name}.gcode.3mf"

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    printer_folder = str(source_archive.printer_id) if source_archive.printer_id is not None else "unassigned"
    archive_subdir = f"{timestamp}_{base_name}_sliced"
    archive_dir = app_settings.archive_dir / printer_folder / archive_subdir
    archive_dir.mkdir(parents=True, exist_ok=True)
    out_path = archive_dir / out_filename
    out_path.write_bytes(result.content)

    thumbnail_path: str | None = None
    parsed_metadata: dict = {}
    try:
        parser = ThreeMFParser(str(out_path))
        parsed = parser.parse()
        thumb_data = parsed.get("_thumbnail_data")
        thumb_ext = parsed.get("_thumbnail_ext", ".png")
        if thumb_data:
            thumb_dest = archive_dir / f"thumbnail{thumb_ext}"
            thumb_dest.write_bytes(thumb_data)
            thumbnail_path = str(thumb_dest.relative_to(app_settings.base_dir))
        parsed_metadata = {k: v for k, v in parsed.items() if not k.startswith("_")}
    except Exception as exc:
        logger.warning("Failed to parse sliced 3MF metadata for %s: %s", out_filename, exc)

    metadata = dict(source_archive.extra_data) if source_archive.extra_data else {}
    metadata.update(parsed_metadata)
    metadata.update(
        {
            "sliced_from_archive_id": source_archive.id,
            "print_time_seconds": result.print_time_seconds,
            "filament_used_g": result.filament_used_g,
            "filament_used_mm": result.filament_used_mm,
        }
    )
    if used_embedded_settings:
        metadata["used_embedded_settings"] = True

    # Prefer the actually-used filament list from the sliced output's
    # slice_info.config (parsed_metadata.filament_* — only entries with
    # used_g > 0). Falling back to the source_archive's list would
    # surface every project-wide AMS slot, including ones the picked
    # plate doesn't use.
    new_filament_type = parsed_metadata.get("filament_type") or source_archive.filament_type
    new_filament_color = parsed_metadata.get("filament_color") or source_archive.filament_color

    new_archive = PrintArchive(
        printer_id=source_archive.printer_id,
        project_id=source_archive.project_id,
        filename=out_filename,
        file_path=str(out_path.relative_to(app_settings.base_dir)),
        file_size=len(result.content),
        content_hash=hashlib.sha256(result.content).hexdigest(),
        thumbnail_path=thumbnail_path,
        # Inherit identity from the source archive so the new entry shows up
        # alongside its sibling in the archives list.
        print_name=(source_archive.print_name or base_name) + " (re-sliced)",
        print_time_seconds=result.print_time_seconds,
        filament_used_grams=result.filament_used_g or None,
        filament_type=new_filament_type,
        filament_color=new_filament_color,
        layer_height=source_archive.layer_height,
        nozzle_diameter=source_archive.nozzle_diameter,
        sliced_for_model=source_archive.sliced_for_model,
        makerworld_url=source_archive.makerworld_url,
        designer=source_archive.designer,
        # Sliced-but-not-printed: keep status default ("completed") so it
        # surfaces in the normal archives list, but do not stamp
        # started/completed_at — the user hasn't actually printed it yet.
        extra_data=metadata,
    )
    db.add(new_archive)
    await db.commit()
    await db.refresh(new_archive)

    return SliceArchiveResponse(
        archive_id=new_archive.id,
        name=new_archive.print_name or out_filename,
        print_time_seconds=result.print_time_seconds,
        filament_used_g=result.filament_used_g,
        filament_used_mm=result.filament_used_mm,
        used_embedded_settings=used_embedded_settings,
    )


@router.post("/files/{file_id}/slice", status_code=202)
async def slice_library_file(
    file_id: int,
    request_body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
    api_key_cloud_owner: User | None = Depends(resolve_api_key_cloud_owner),
):
    """Enqueue a slice job for a library file. Returns 202 + job_id; the
    slice runs in the background, the caller polls ``GET /slice-jobs/{id}``.

    When the caller authenticates via API key, ``current_user`` is None.
    ``api_key_cloud_owner`` resolves to the key's owner *only* when
    ``can_access_cloud=True`` is set on that key — that lets cloud presets
    referenced by the slice request (printer/process/filament IDs in the
    ``cloud:...`` form) bind to the owner's per-user Bambu Cloud token.
    Without an owner, the slicer falls through to local + bundled presets;
    cloud-only presets fail upstream with the existing "preset not found"
    error (#1182).
    """
    from backend.app.core.database import async_session
    from backend.app.schemas.slicer import SliceRequest
    from backend.app.services.slice_dispatch import (
        http_exception_to_job_error,
        slice_dispatch,
    )

    # Validate the body via the schema explicitly so the route's loose
    # ``dict`` annotation doesn't bypass the validator's preset normalisation
    # + multi-slot promotion logic.
    try:
        request = SliceRequest.model_validate(request_body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    src_result = await db.execute(LibraryFile.active().where(LibraryFile.id == file_id))
    lib_file = src_result.scalar_one_or_none()
    if not lib_file:
        raise HTTPException(status_code=404, detail="File not found")

    src_lower = (lib_file.filename or "").lower()
    if not (
        src_lower.endswith(".stl")
        or src_lower.endswith(".3mf")
        or src_lower.endswith(".step")
        or src_lower.endswith(".stp")
    ):
        raise HTTPException(status_code=400, detail="Source file must be STL, 3MF, or STEP")

    src_path = to_absolute_path(lib_file.file_path)
    if not src_path or not src_path.exists():
        raise HTTPException(status_code=404, detail="Source file missing on disk")

    # Capture inputs the bg task needs — the request DB session is closed
    # before the background task runs.
    model_bytes = src_path.read_bytes()
    folder_id = lib_file.folder_id
    source_lib_file_id = lib_file.id
    # JWT user wins; fall back to the API key's owner so a cloud-scoped key
    # spends *that* user's token (#1182). The id is what the bg task needs —
    # it re-loads the User in its own session to look up cloud creds.
    cloud_token_user = current_user or api_key_cloud_owner
    user_id = cloud_token_user.id if cloud_token_user else None

    # If the source has a ``print_name`` in its metadata (BambuStudio always
    # sets this; OrcaSlicer often leaves it blank), derive the sliced
    # output's filename from it instead of the raw filename. The source
    # row's display already prefers print_name, so the sliced row's
    # filename ("Piggo the piggy bank.gcode.3mf") will match the source's
    # display name ("Piggo the piggy bank") with the gcode extension added.
    src_print_name = None
    if lib_file.file_metadata:
        candidate = lib_file.file_metadata.get("print_name")
        if isinstance(candidate, str) and candidate.strip():
            src_print_name = candidate.strip()
    src_ext = Path(lib_file.filename).suffix.lower() or ".3mf"
    model_filename = f"{src_print_name}{src_ext}" if src_print_name else lib_file.filename

    async def _run(job_id: int):
        async with async_session() as task_db:
            try:
                response = await slice_and_persist(
                    task_db,
                    model_bytes=model_bytes,
                    model_filename=model_filename,
                    folder_id=folder_id,
                    extra_metadata={"sliced_from_library_file_id": source_lib_file_id},
                    request=request,
                    current_user_id=user_id,
                    job_id=job_id,
                )
            except HTTPException as exc:
                raise http_exception_to_job_error(exc) from exc
        return response.model_dump()

    job = await slice_dispatch.enqueue(
        kind="library_file",
        source_id=lib_file.id,
        source_name=lib_file.filename,
        run=_run,
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "status_url": f"/api/v1/slice-jobs/{job.id}",
    }


@router.post("/files", response_model=FileUploadResponse)
@router.post("/files/", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    folder_id: int | None = None,
    generate_stl_thumbnails: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
):
    """Upload a file to the library."""
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="Filename is required")

        filename = file.filename
        ext = os.path.splitext(filename)[1].lower()
        file_type = detect_file_type(filename)

        # Verify folder exists if specified. Eager-load .projects so a
        # subsequent ``inherit_folder_projects`` call doesn't trip the
        # async lazy-load — the file inherits the folder's projects so the
        # print plan auto-fills (#m048 + post-m044 fix).
        target_folder: LibraryFolder | None = None
        if folder_id is not None:
            folder_result = await db.execute(
                select(LibraryFolder).where(LibraryFolder.id == folder_id).options(selectinload(LibraryFolder.projects))
            )
            target_folder = folder_result.scalar_one_or_none()
            if not target_folder:
                raise HTTPException(status_code=404, detail="Folder not found")

        # Writable external folders write through to the mount so the file is
        # visible outside BamDude (upstream #1112); everything else lands under
        # the internal library dir with a UUID-scoped filename.
        file_path, is_external_upload = _resolve_upload_destination(target_folder, filename)

        # Save file
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)

        # Calculate hash
        file_hash = calculate_file_hash(file_path)

        # Check for duplicates — only against active (non-trashed) files. A
        # trashed sibling has been deleted by the user and shouldn't pin a
        # fresh upload to it.
        dup_result = await db.execute(
            select(LibraryFile.id).where(LibraryFile.file_hash == file_hash, LibraryFile.deleted_at.is_(None)).limit(1)
        )
        duplicate_of = dup_result.scalar()

        # Extract metadata and thumbnail
        metadata = {}
        thumbnail_path = None
        thumbnails_dir = get_library_thumbnails_dir()

        if ext == ".3mf":
            try:
                parser = ThreeMFParser(str(file_path))
                raw_metadata = parser.parse()

                # Extract thumbnail before cleaning metadata
                thumbnail_data = raw_metadata.get("_thumbnail_data")
                thumbnail_ext = raw_metadata.get("_thumbnail_ext", ".png")

                # Save thumbnail if extracted
                if thumbnail_data:
                    thumb_filename = f"{uuid.uuid4().hex}{thumbnail_ext}"
                    thumb_path = thumbnails_dir / thumb_filename
                    with open(thumb_path, "wb") as f:
                        f.write(thumbnail_data)
                    thumbnail_path = str(thumb_path)

                metadata = _clean_3mf_metadata(raw_metadata)

                # Populate per-plate cache so the gallery / list endpoint
                # doesn't need to reopen the ZIP on every read. ``plates``
                # carries the full per-plate breakdown; ``is_multi_plate``
                # is a tiny top-level boolean that the file-list response
                # uses to gate gallery rendering on the frontend.
                try:
                    import zipfile as _zf

                    from backend.app.services.archive import parse_plates_from_3mf

                    with _zf.ZipFile(str(file_path), "r") as _zfh:
                        plates_payload = parse_plates_from_3mf(_zfh)
                    if plates_payload:
                        metadata["plates"] = plates_payload
                        metadata["is_multi_plate"] = len(plates_payload) > 1
                except Exception as _pe:
                    logger.debug("Per-plate parse for upload failed (non-critical): %s", _pe)
            except Exception as e:
                logger.warning("Failed to parse 3MF: %s", e)

        elif ext == ".gcode":
            # Extract embedded thumbnail from gcode
            try:
                thumbnail_data = extract_gcode_thumbnail(file_path)
                if thumbnail_data:
                    thumb_filename = f"{uuid.uuid4().hex}.png"
                    thumb_path = thumbnails_dir / thumb_filename
                    with open(thumb_path, "wb") as f:
                        f.write(thumbnail_data)
                    thumbnail_path = str(thumb_path)
            except Exception as e:
                logger.warning("Failed to extract gcode thumbnail: %s", e)

        elif ext.lower() in IMAGE_EXTENSIONS:
            # For image files, create a thumbnail from the image itself
            thumbnail_path = create_image_thumbnail(file_path, thumbnails_dir)

        elif ext == ".stl":
            # Generate STL thumbnail if enabled
            if generate_stl_thumbnails:
                thumbnail_path = generate_stl_thumbnail(file_path, thumbnails_dir)

        # Detect swap mode compatibility from filename. Covers both the
        # singular ".swap." suffix (older / custom tooling) and the ".swaps."
        # suffix that swaplist.app actually emits on export.
        fname_lower = filename.lower()
        swap_compatible = (
            fname_lower.endswith((".swap.3mf", ".swaps.3mf")) or ".swap." in fname_lower or ".swaps." in fname_lower
        )

        # Create database entry (managed files store relative paths for
        # portability; external files store the absolute mount path — same
        # shape scan produces).
        library_file = LibraryFile(
            folder_id=folder_id,
            is_external=is_external_upload,
            filename=filename,
            file_path=_stored_file_path(file_path, is_external_upload),
            file_type=file_type,
            file_tags=compute_file_tags(
                filename=filename,
                file_type=file_type,
                file_metadata=metadata if metadata else None,
                source_type=None,
                swap_compatible=swap_compatible,
            ),
            file_size=len(content),
            file_hash=file_hash,
            thumbnail_path=to_relative_path(thumbnail_path) if thumbnail_path else None,
            file_metadata=metadata if metadata else None,
            created_by_id=current_user.id if current_user else None,
            swap_compatible=swap_compatible,
        )
        db.add(library_file)
        await db.flush()
        # Inherit the target folder's projects + plant matching print-plan
        # rows so a 3MF dropped into a project-tagged folder shows up in
        # the project's plan automatically.
        await inherit_folder_projects(db, library_file, target_folder)
        await db.commit()
        await db.refresh(library_file)

        return FileUploadResponse(
            id=library_file.id,
            filename=library_file.filename,
            file_type=library_file.file_type,
            file_tags=library_file.file_tags or [],
            file_size=library_file.file_size,
            thumbnail_path=library_file.thumbnail_path,
            duplicate_of=duplicate_of,
            metadata=library_file.file_metadata,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Upload failed for %s: %s", file.filename, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.post("/files/extract-zip", response_model=ZipExtractResponse)
async def extract_zip_file(
    file: UploadFile = File(...),
    folder_id: int | None = Query(default=None),
    preserve_structure: bool = Query(default=True),
    create_folder_from_zip: bool = Query(default=False),
    generate_stl_thumbnails: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
):
    """Upload and extract a ZIP file to the library.

    Args:
        file: The ZIP file to extract
        folder_id: Target folder ID (None = root)
        preserve_structure: If True, recreate folder structure from ZIP; if False, extract all files flat
        create_folder_from_zip: If True, create a folder named after the ZIP file and extract into it
        generate_stl_thumbnails: If True, generate thumbnails for STL files
    """
    import tempfile

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only ZIP files are supported")

    # Verify target folder exists if specified. Eager-load .projects so the
    # inherit-on-create path below doesn't trip the async lazy-load.
    if folder_id is not None:
        folder_result = await db.execute(
            select(LibraryFolder).where(LibraryFolder.id == folder_id).options(selectinload(LibraryFolder.projects))
        )
        target_folder = folder_result.scalar_one_or_none()
        if not target_folder:
            raise HTTPException(status_code=404, detail="Target folder not found")
        if target_folder.is_external and target_folder.external_readonly:
            raise HTTPException(status_code=403, detail="Cannot extract ZIP to a read-only external folder")

    # Save ZIP to temp file
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save ZIP file: {str(e)}")

    extracted_files: list[ZipExtractResult] = []
    errors: list[ZipExtractError] = []
    folders_created = 0
    folder_cache: dict[str, int] = {}  # path -> folder_id

    # If create_folder_from_zip is True, create a folder named after the ZIP file
    zip_folder_id = folder_id
    logger.info(
        f"ZIP extraction: create_folder_from_zip={create_folder_from_zip}, folder_id={folder_id}, filename={file.filename}"
    )
    if create_folder_from_zip and file.filename:
        # Remove .zip extension to get folder name
        zip_folder_name = file.filename[:-4] if file.filename.lower().endswith(".zip") else file.filename
        # Check if folder already exists
        existing = await db.execute(
            select(LibraryFolder).where(
                LibraryFolder.name == zip_folder_name,
                LibraryFolder.parent_id == folder_id if folder_id else LibraryFolder.parent_id.is_(None),
            )
        )
        existing_folder = existing.scalar_one_or_none()
        if existing_folder:
            zip_folder_id = existing_folder.id
            logger.info("Reusing existing folder '%s' with id=%s", zip_folder_name, zip_folder_id)
        else:
            # Create folder
            new_folder = LibraryFolder(name=zip_folder_name, parent_id=folder_id)
            db.add(new_folder)
            await db.flush()
            await db.commit()  # Commit folder creation immediately
            zip_folder_id = new_folder.id
            folders_created += 1
            logger.info("Created new folder '%s' with id=%s", zip_folder_name, zip_folder_id)

    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            # Filter out directories and hidden/system files
            file_list = [
                name
                for name in zf.namelist()
                if not name.endswith("/")
                and not name.startswith("__MACOSX")
                and not os.path.basename(name).startswith(".")
            ]

            for zip_path in file_list:
                try:
                    # Determine target folder (use zip_folder_id as base if create_folder_from_zip was used)
                    target_folder_id = zip_folder_id

                    if preserve_structure:
                        # Get directory path from ZIP
                        dir_path = os.path.dirname(zip_path)
                        if dir_path:
                            # Create folder structure
                            parts = dir_path.split("/")
                            current_parent = zip_folder_id
                            current_path = ""

                            for part in parts:
                                if not part:
                                    continue
                                current_path = f"{current_path}/{part}" if current_path else part

                                if current_path in folder_cache:
                                    current_parent = folder_cache[current_path]
                                else:
                                    # Check if folder exists
                                    existing = await db.execute(
                                        select(LibraryFolder).where(
                                            LibraryFolder.name == part,
                                            LibraryFolder.parent_id == current_parent
                                            if current_parent
                                            else LibraryFolder.parent_id.is_(None),
                                        )
                                    )
                                    existing_folder = existing.scalar_one_or_none()

                                    if existing_folder:
                                        current_parent = existing_folder.id
                                    else:
                                        # Create folder
                                        new_folder = LibraryFolder(name=part, parent_id=current_parent)
                                        db.add(new_folder)
                                        await db.flush()
                                        current_parent = new_folder.id
                                        folders_created += 1

                                    folder_cache[current_path] = current_parent

                            target_folder_id = current_parent

                    # Extract file
                    filename = os.path.basename(zip_path)
                    ext = os.path.splitext(filename)[1].lower()
                    file_type = detect_file_type(filename)

                    # Generate unique filename for storage
                    unique_filename = f"{uuid.uuid4().hex}{ext}"
                    file_path = get_library_files_dir() / unique_filename

                    # Extract and save file
                    file_content = zf.read(zip_path)
                    with open(file_path, "wb") as f:
                        f.write(file_content)

                    # Calculate hash
                    file_hash = calculate_file_hash(file_path)

                    # Extract metadata and thumbnail for 3MF files
                    metadata = {}
                    thumbnail_path = None
                    thumbnails_dir = get_library_thumbnails_dir()

                    if ext == ".3mf":
                        try:
                            parser = ThreeMFParser(str(file_path))
                            raw_metadata = parser.parse()

                            thumbnail_data = raw_metadata.get("_thumbnail_data")
                            thumbnail_ext = raw_metadata.get("_thumbnail_ext", ".png")

                            if thumbnail_data:
                                thumb_filename = f"{uuid.uuid4().hex}{thumbnail_ext}"
                                thumb_path = thumbnails_dir / thumb_filename
                                with open(thumb_path, "wb") as f:
                                    f.write(thumbnail_data)
                                thumbnail_path = str(thumb_path)

                            metadata = _clean_3mf_metadata(raw_metadata)
                            # Per-plate cache (same as upload_file path).
                            try:
                                import zipfile as _zf

                                from backend.app.services.archive import parse_plates_from_3mf

                                with _zf.ZipFile(str(file_path), "r") as _zfh:
                                    plates_payload = parse_plates_from_3mf(_zfh)
                                if plates_payload:
                                    metadata["plates"] = plates_payload
                                    metadata["is_multi_plate"] = len(plates_payload) > 1
                            except Exception as _pe:
                                logger.debug("Per-plate parse for ZIP-extracted 3MF failed (non-critical): %s", _pe)
                        except Exception as e:
                            logger.warning("Failed to parse 3MF from ZIP: %s", e)

                    elif ext == ".gcode":
                        try:
                            thumbnail_data = extract_gcode_thumbnail(file_path)
                            if thumbnail_data:
                                thumb_filename = f"{uuid.uuid4().hex}.png"
                                thumb_path = thumbnails_dir / thumb_filename
                                with open(thumb_path, "wb") as f:
                                    f.write(thumbnail_data)
                                thumbnail_path = str(thumb_path)
                        except Exception as e:
                            logger.warning("Failed to extract gcode thumbnail from ZIP: %s", e)

                    elif ext.lower() in IMAGE_EXTENSIONS:
                        thumbnail_path = create_image_thumbnail(file_path, thumbnails_dir)

                    elif ext == ".stl":
                        # Generate STL thumbnail if enabled
                        if generate_stl_thumbnails:
                            thumbnail_path = generate_stl_thumbnail(file_path, thumbnails_dir)

                    # Create database entry (store relative paths for portability)
                    library_file = LibraryFile(
                        folder_id=target_folder_id,
                        filename=filename,
                        file_path=to_relative_path(file_path),
                        file_type=file_type,
                        file_tags=compute_file_tags(
                            filename=filename,
                            file_type=file_type,
                            file_metadata=metadata if metadata else None,
                            source_type=None,
                            swap_compatible=False,
                        ),
                        file_size=len(file_content),
                        file_hash=file_hash,
                        thumbnail_path=to_relative_path(thumbnail_path) if thumbnail_path else None,
                        file_metadata=metadata if metadata else None,
                        created_by_id=current_user.id if current_user else None,
                    )
                    db.add(library_file)
                    await db.flush()
                    # Inherit target folder's projects → matching plan rows
                    # so a 3MF unzipped into a project-tagged folder lands
                    # in the project's plan automatically. Re-fetch the
                    # folder with .projects eager-loaded for *this*
                    # iteration's target_folder_id (may differ from the
                    # outer one when ``preserve_structure`` created a
                    # subfolder; subfolders inherit their parent's
                    # projects only when explicitly assigned, so this is
                    # a no-op for sub-folders that have no projects of
                    # their own).
                    if target_folder_id is not None:
                        per_file_folder = (
                            await db.execute(
                                select(LibraryFolder)
                                .where(LibraryFolder.id == target_folder_id)
                                .options(selectinload(LibraryFolder.projects))
                            )
                        ).scalar_one_or_none()
                        if per_file_folder is not None:
                            await inherit_folder_projects(db, library_file, per_file_folder)
                    await db.refresh(library_file)

                    extracted_files.append(
                        ZipExtractResult(
                            filename=filename,
                            file_id=library_file.id,
                            folder_id=target_folder_id,
                        )
                    )

                    # Commit after each file to release database lock
                    # This prevents long-running transactions from blocking other requests
                    await db.commit()

                except Exception as e:
                    logger.error("Failed to extract %s: %s", zip_path, e)
                    errors.append(ZipExtractError(filename=os.path.basename(zip_path), error=str(e)))
                    # Rollback the failed file but continue with others
                    await db.rollback()

        return ZipExtractResponse(
            extracted=len(extracted_files),
            folders_created=folders_created,
            files=extracted_files,
            errors=errors,
        )

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid or corrupted ZIP file")
    except Exception as e:
        logger.error("ZIP extraction failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"ZIP extraction failed: {str(e)}")
    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # Best-effort temp file cleanup; ignore if already removed


# ============ STL Thumbnail Batch Generation ============


@router.post("/generate-stl-thumbnails", response_model=BatchThumbnailResponse)
async def batch_generate_stl_thumbnails(
    request: BatchThumbnailRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_UPDATE_ALL)),
):
    """Generate thumbnails for STL files in batch.

    Note: Requires library:update_all permission since this is a batch operation
    that may affect files owned by different users.

    Can generate thumbnails for:
    - Specific file IDs (file_ids)
    - All STL files in a folder (folder_id)
    - All STL files missing thumbnails (all_missing=True)
    """
    thumbnails_dir = get_library_thumbnails_dir()
    results: list[BatchThumbnailResult] = []

    # Build query based on request (trash-aware: skip soft-deleted rows)
    query = LibraryFile.active().where(LibraryFile.file_type == "stl")

    if request.file_ids:
        # Specific files
        query = query.where(LibraryFile.id.in_(request.file_ids))
    elif request.folder_id is not None:
        # All STL files in a specific folder
        query = query.where(LibraryFile.folder_id == request.folder_id)
        if not request.all_missing:
            # If not specifically asking for missing thumbnails, get all
            pass
        else:
            query = query.where(LibraryFile.thumbnail_path.is_(None))
    elif request.all_missing:
        # All STL files without thumbnails
        query = query.where(LibraryFile.thumbnail_path.is_(None))
    else:
        # No criteria specified - return empty
        return BatchThumbnailResponse(
            processed=0,
            succeeded=0,
            failed=0,
            results=[],
        )

    result = await db.execute(query)
    stl_files = result.scalars().all()

    succeeded = 0
    failed = 0

    for stl_file in stl_files:
        file_path = to_absolute_path(stl_file.file_path)

        if not file_path or not file_path.exists():
            results.append(
                BatchThumbnailResult(
                    file_id=stl_file.id,
                    filename=stl_file.filename,
                    success=False,
                    error="File not found on disk",
                )
            )
            failed += 1
            continue

        try:
            thumbnail_path = generate_stl_thumbnail(file_path, thumbnails_dir)

            if thumbnail_path:
                # Update database with relative path
                stl_file.thumbnail_path = to_relative_path(thumbnail_path)
                await db.flush()
                results.append(
                    BatchThumbnailResult(
                        file_id=stl_file.id,
                        filename=stl_file.filename,
                        success=True,
                    )
                )
                succeeded += 1
            else:
                results.append(
                    BatchThumbnailResult(
                        file_id=stl_file.id,
                        filename=stl_file.filename,
                        success=False,
                        error="Thumbnail generation failed",
                    )
                )
                failed += 1
        except Exception as e:
            logger.error("Failed to generate thumbnail for %s: %s", stl_file.filename, e)
            results.append(
                BatchThumbnailResult(
                    file_id=stl_file.id,
                    filename=stl_file.filename,
                    success=False,
                    error=str(e),
                )
            )
            failed += 1

    await db.commit()

    return BatchThumbnailResponse(
        processed=len(stl_files),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )


# ============ Queue Operations ============
# NOTE: These routes must be defined BEFORE /files/{file_id} to avoid path parameter conflicts


def is_sliced_file(filename: str) -> bool:
    """Check if a file is a sliced (printable) file.

    Sliced files are:
    - .gcode files
    - .3mf files that contain '.gcode.' in the name (e.g., filename.gcode.3mf)
    """
    lower = filename.lower()
    return lower.endswith(".gcode") or ".gcode." in lower


@router.post("/files/add-to-queue", response_model=AddToQueueResponse)
async def add_files_to_queue(
    request: AddToQueueRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.QUEUE_CREATE)),
):
    """Add library files to the print queue.

    Only sliced files (.gcode or .gcode.3mf) can be added to the queue.
    The archive will be created automatically when the print starts.
    """
    added: list[AddToQueueResult] = []
    errors: list[AddToQueueError] = []

    # Get all requested files. Eager-load M2M projects so the per-item
    # "first project as queue project_id" fallback below doesn't lazy-load.
    result = await db.execute(
        select(LibraryFile).where(LibraryFile.id.in_(request.file_ids)).options(selectinload(LibraryFile.projects))
    )
    files = {f.id: f for f in result.scalars().all()}

    # Get max position for queue ordering
    pos_result = await db.execute(select(func.coalesce(func.max(PrintQueueItem.position), 0)))
    max_position = pos_result.scalar() or 0

    for file_id in request.file_ids:
        lib_file = files.get(file_id)

        if not lib_file:
            errors.append(AddToQueueError(file_id=file_id, filename="(not found)", error="File not found"))
            continue

        # Validate file is sliced
        if not is_sliced_file(lib_file.filename):
            errors.append(
                AddToQueueError(
                    file_id=file_id,
                    filename=lib_file.filename,
                    error="Not a sliced file. Only .gcode or .gcode.3mf files can be printed.",
                )
            )
            continue

        try:
            # Verify file exists on disk
            file_path = Path(app_settings.base_dir) / lib_file.file_path

            if not file_path.exists():
                errors.append(
                    AddToQueueError(file_id=file_id, filename=lib_file.filename, error="File not found on disk")
                )
                continue

            # Create queue item referencing library file (archive created at print start).
            # Queue items stay single-project. m044: a file may belong to
            # multiple projects — pick the first as a fallback so project
            # stats still count the item when bulk-added from File Manager
            # with no explicit project context. Operators wanting a
            # specific project should pass it via the per-printer queue
            # add flow instead of bulk-add.
            max_position += 1
            inherited_project_id = lib_file.projects[0].id if lib_file.projects else None
            queue_item = PrintQueueItem(
                printer_id=None,  # Unassigned
                library_file_id=file_id,
                project_id=inherited_project_id,
                position=max_position,
                status="pending",
                created_by_id=current_user.id if current_user else None,
            )
            db.add(queue_item)

            await db.flush()  # Get queue_item.id

            added.append(
                AddToQueueResult(
                    file_id=file_id,
                    filename=lib_file.filename,
                    queue_item_id=queue_item.id,
                )
            )

        except Exception as e:
            logger.exception("Error adding file %s to queue", file_id)
            errors.append(AddToQueueError(file_id=file_id, filename=lib_file.filename, error=str(e)))

    await db.commit()

    return AddToQueueResponse(added=added, errors=errors)


@router.get("/files/{file_id}/plates")
async def get_library_file_plates(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get available plates from a multi-plate 3MF library file.

    Returns a list of plates with their index, name, thumbnail availability,
    and filament requirements. For single-plate exports, returns a single plate.
    """

    # Get the library file
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    lib_file = result.scalar_one_or_none()

    if not lib_file:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(app_settings.base_dir) / lib_file.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    # Only 3MF files have plates
    if not lib_file.filename.lower().endswith(".3mf"):
        return {
            "file_id": file_id,
            "filename": lib_file.filename,
            "plates": [],
            "is_multi_plate": False,
            "source_printer_model": None,
        }

    # SliceModal pre-check signal: the source 3MF's bound printer model. The
    # slicer CLI cannot re-slice for a different printer; surface this so
    # the modal can warn the user before they pick a mismatched profile.
    source_printer_model: str | None = None
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            source_printer_model = extract_source_printer_model_from_3mf(zf)
    except (zipfile.BadZipFile, OSError):
        pass

    # Fast path: read pre-computed plates from the library file's JSON
    # metadata (populated at upload time + by m023 backfill). No ZIP open.
    cached_plates = (lib_file.file_metadata or {}).get("plates") if isinstance(lib_file.file_metadata, dict) else None
    if isinstance(cached_plates, list) and cached_plates:
        plates = [
            {
                **p,
                "thumbnail_url": (
                    f"/api/v1/library/files/{file_id}/plate-thumbnail/{p.get('index')}"
                    if p.get("has_thumbnail")
                    else None
                ),
            }
            for p in cached_plates
        ]
        return {
            "file_id": file_id,
            "filename": lib_file.filename,
            "plates": plates,
            "is_multi_plate": len(plates) > 1,
            "source_printer_model": source_printer_model,
        }

    # Slow path: open ZIP + parse. Used for files uploaded before m023 ran,
    # or as a safety net after a corrupted JSON column. Result is NOT
    # written back here — the migration / upload-time hook owns persistence.
    plates: list[dict] = []
    try:
        from backend.app.services.archive import parse_plates_from_3mf

        with zipfile.ZipFile(file_path, "r") as zf:
            raw_plates = parse_plates_from_3mf(zf)
        for p in raw_plates:
            plates.append(
                {
                    **p,
                    "thumbnail_url": (
                        f"/api/v1/library/files/{file_id}/plate-thumbnail/{p['index']}" if p["has_thumbnail"] else None
                    ),
                }
            )
    except Exception as e:
        logger.warning("Failed to parse plates from library file %s: %s", file_id, e)

    return {
        "file_id": file_id,
        "filename": lib_file.filename,
        "plates": plates,
        "is_multi_plate": len(plates) > 1,
        "source_printer_model": source_printer_model,
    }


@router.get("/files/{file_id}/plate-thumbnail/{plate_index}")
async def get_library_file_plate_thumbnail(
    file_id: int,
    plate_index: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the thumbnail image for a specific plate from a library file."""
    from starlette.responses import Response

    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    lib_file = result.scalar_one_or_none()

    if not lib_file:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(app_settings.base_dir) / lib_file.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            thumb_path = f"Metadata/plate_{plate_index}.png"
            if thumb_path in zf.namelist():
                data = zf.read(thumb_path)
                return Response(content=data, media_type="image/png")
    except Exception:
        pass  # Archive unreadable or thumbnail missing; fall through to 404

    raise HTTPException(status_code=404, detail=f"Thumbnail for plate {plate_index} not found")


async def _try_preview_slice_filaments(
    db: AsyncSession,
    *,
    kind: str,
    source_id: int,
    plate_id: int,
    file_path: Path,
    request_id: str | None = None,
) -> list[dict] | None:
    """Run a preview slice via the user's configured sidecar. Same shape as
    the matching helper in archives.py — see that module for rationale.

    ``request_id``: when supplied, forwarded to the sidecar so the
    SliceModal's inline spinner + toast can poll the matching progress
    endpoint and show "Generating G-code (45%)" for the preview as well.

    The preview always uses the global ``preferred_slicer`` setting (no
    per-request override): the modal calls this BEFORE the user has chosen
    a slicer, just to discover which AMS slots the plate touches. Routing
    by the user's pick would mean two preview slices on different sidecars
    if the user switches the radio.
    """
    from backend.app.services.slice_preview import get_preview_filaments
    from backend.app.services.slicer_routing import resolve_sidecar_url

    _, api_url = await resolve_sidecar_url(db)
    if not api_url:
        return None

    try:
        file_bytes = file_path.read_bytes()
    except OSError:
        return None
    return await get_preview_filaments(
        kind=kind,
        source_id=source_id,
        plate_id=plate_id,
        file_bytes=file_bytes,
        file_name=file_path.name,
        api_url=api_url,
        request_id=request_id,
    )


@router.get("/files/{file_id}/capabilities")
async def get_library_file_capabilities(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Viewer capabilities for a library file.

    Tab visibility is driven by ``file_tags`` (m036) — the canonical
    identity vocabulary the rest of the file manager already uses:

    - ``gcode`` tag → G-code preview tab is meaningful (raw .gcode OR
      sliced .gcode.3mf).
    - ``project`` (unsliced .3mf project package) OR ``geometry`` (raw
      mesh / CAD source — STL / OBJ / STEP) → 3D model tab is
      meaningful.
    - Sliced .gcode.3mf carries ``gcode`` + ``3mf`` but NOT ``project``
      (the slicer rasterised the mesh into G-code lines), so it shows
      only the G-code tab. This mirrors the archive route's policy of
      not duplicating the mesh under "3D Model" when the bytes the
      user would see there are already painted by the gcode preview.

    Build volume + filament colours are parsed from the 3MF container
    (when one exists) so the bed grid + extrusion colours under the
    preview match the source. Non-container rows (.gcode / .stl /
    .obj / .step) fall back to the X1/P1/A1 default volume and an
    empty colour list — the file format itself carries no machine
    config to extract.
    """
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    file = result.scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    tags = set(file.file_tags or [])
    has_gcode = "gcode" in tags
    has_model = "project" in tags or "geometry" in tags

    # 3MF container? Worth parsing for build_volume + filament_colors.
    # Includes both unsliced .3mf (carries ``3mf`` tag) and sliced
    # .gcode.3mf (carries ``gcode`` + ``3mf``).
    has_3mf_container = "3mf" in tags
    default_volume = {"x": 256, "y": 256, "z": 256}

    if not has_3mf_container:
        return {
            "has_model": has_model,
            "has_gcode": has_gcode,
            "has_source": False,
            "build_volume": default_volume,
            "filament_colors": [],
        }

    abs_path = to_absolute_path(file.file_path)
    if not abs_path or not abs_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    try:
        caps = extract_3mf_capabilities(primary_path=abs_path)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Invalid 3MF file") from exc

    return {
        # Tag-based, not probe-based — sliced .gcode.3mf with embedded
        # mesh entries should still hide the 3D tab per the policy
        # documented above.
        "has_model": has_model,
        "has_gcode": has_gcode,
        # Library files have no separate "source 3MF" sidecar — the row
        # IS the source. Always False here so the frontend doesn't try
        # to fetch a non-existent /source endpoint.
        "has_source": False,
        "build_volume": caps.build_volume,
        "filament_colors": caps.filament_colors,
    }


@router.get("/files/{file_id}/filament-requirements")
async def get_library_file_filament_requirements(
    file_id: int,
    plate_id: int | None = None,
    request_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get filament requirements from a library file.

    Parses the 3MF file to extract filament slot IDs, types, colors, and usage.
    This enables AMS slot assignment when printing from the file manager.

    Args:
        file_id: The library file ID
        plate_id: Optional plate index to get filaments for a specific plate
        request_id: forwarded to the sidecar's preview-slice fallback for
            unsliced project files; lets the SliceModal's inline spinner +
            toast poll matching live progress.
    """
    import defusedxml.ElementTree as ET

    # Get the library file
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    lib_file = result.scalar_one_or_none()

    if not lib_file:
        raise HTTPException(status_code=404, detail="File not found")

    # Get the full file path
    file_path = Path(app_settings.base_dir) / lib_file.file_path

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    # Only 3MF files have parseable filament info
    if not lib_file.filename.lower().endswith(".3mf"):
        return {"file_id": file_id, "filename": lib_file.filename, "plate_id": plate_id, "filaments": []}

    filaments = []

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Parse slice_info.config for filament requirements
            if "Metadata/slice_info.config" in zf.namelist():
                content = zf.read("Metadata/slice_info.config").decode()
                root = ET.fromstring(content)

                if plate_id is not None:
                    # Find filaments for specific plate
                    for plate_elem in root.findall(".//plate"):
                        # Check if this is the requested plate
                        plate_index = None
                        for meta in plate_elem.findall("metadata"):
                            if meta.get("key") == "index":
                                try:
                                    plate_index = int(meta.get("value", ""))
                                except ValueError:
                                    pass  # Skip plate with non-numeric index value
                                break

                        if plate_index == plate_id:
                            # Extract filaments from this plate
                            for filament_elem in plate_elem.findall("filament"):
                                filament_id = filament_elem.get("id")
                                filament_type = filament_elem.get("type", "")
                                filament_color = filament_elem.get("color", "")
                                used_g = filament_elem.get("used_g", "0")
                                used_m = filament_elem.get("used_m", "0")

                                tray_info_idx = filament_elem.get("tray_info_idx", "")

                                try:
                                    used_grams = float(used_g)
                                except (ValueError, TypeError):
                                    used_grams = 0

                                if used_grams > 0 and filament_id:
                                    filaments.append(
                                        {
                                            "slot_id": int(filament_id),
                                            "type": filament_type,
                                            "color": filament_color,
                                            "used_grams": round(used_grams, 1),
                                            "used_meters": float(used_m) if used_m else 0,
                                            "tray_info_idx": tray_info_idx,
                                            # Sliced output already pre-filtered by used_g>0,
                                            # so every entry that survives is in fact used by
                                            # this plate. SliceModal uses the flag to enable/
                                            # disable rows; print-dispatch consumers ignore it.
                                            "used_in_plate": True,
                                        }
                                    )
                            break
                else:
                    # Extract all filaments with used_g > 0 (for single-plate or overview)
                    for filament_elem in root.findall(".//filament"):
                        filament_id = filament_elem.get("id")
                        filament_type = filament_elem.get("type", "")
                        filament_color = filament_elem.get("color", "")
                        used_g = filament_elem.get("used_g", "0")
                        used_m = filament_elem.get("used_m", "0")

                        tray_info_idx = filament_elem.get("tray_info_idx", "")

                        try:
                            used_grams = float(used_g)
                        except (ValueError, TypeError):
                            used_grams = 0

                        if used_grams > 0 and filament_id:
                            filaments.append(
                                {
                                    "slot_id": int(filament_id),
                                    "type": filament_type,
                                    "color": filament_color,
                                    "used_grams": round(used_grams, 1),
                                    "used_meters": float(used_m) if used_m else 0,
                                    "tray_info_idx": tray_info_idx,
                                    "used_in_plate": True,
                                }
                            )

            # Unsliced project files: slice_info had no per-plate data.
            # Return the FULL project_settings.config AMS slot list so the
            # slicer CLI receives a profile for every project slot
            # (otherwise it silently fills the gap from embedded defaults
            # — the source's grey support filament leaks into the output
            # even when the user picked white). Use the preview slice to
            # mark which slots the picked plate actually consumes; the
            # SliceModal disables the unused rows so the user only
            # interacts with the dropdowns that matter, while the backend
            # still has the complete list to pass to the CLI.
            if not filaments:
                with zipfile.ZipFile(file_path, "r") as zf2:
                    project_filaments = extract_project_filaments_from_3mf(zf2)
                used_slot_ids: set[int] = set()
                if project_filaments and plate_id is not None:
                    preview = await _try_preview_slice_filaments(
                        db,
                        kind="library_file",
                        source_id=file_id,
                        plate_id=plate_id,
                        file_path=file_path,
                        request_id=request_id,
                    )
                    if preview is not None:
                        used_slot_ids = {f["slot_id"] for f in preview}
                # Default to "every slot is used" when preview-slice didn't
                # produce data: better to over-enable dropdowns than
                # under-enable and leave the user unable to pick a filament
                # the plate actually uses.
                fallback_all_used = not used_slot_ids
                for f in project_filaments:
                    f["used_in_plate"] = fallback_all_used or f["slot_id"] in used_slot_ids
                filaments = project_filaments

            # Sort by slot ID
            filaments.sort(key=lambda x: x["slot_id"])

            # Enrich with nozzle mapping for dual-nozzle printers
            nozzle_mapping = extract_nozzle_mapping_from_3mf(zf)
            if nozzle_mapping:
                for filament in filaments:
                    filament["nozzle_id"] = nozzle_mapping.get(filament["slot_id"])

    except Exception as e:
        logger.warning("Failed to parse filament requirements from library file %s: %s", file_id, e)

    return {
        "file_id": file_id,
        "filename": lib_file.filename,
        "plate_id": plate_id,
        "filaments": filaments,
    }


@router.post("/files/{file_id}/print")
async def print_library_file(
    file_id: int,
    printer_id: int,
    body: FilePrintRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(require_permission(Permission.PRINTERS_CONTROL)),
):
    """Dispatch a library file for send/start on a printer.

    The actual send/start work is handled asynchronously by background
    dispatch so the UI can continue immediately.

    Only sliced files (.gcode or .gcode.3mf) can be printed.
    """
    from backend.app.models.printer import Printer
    from backend.app.services.background_dispatch import DispatchEnqueueRejected, background_dispatch
    from backend.app.services.printer_manager import printer_manager

    # Use defaults if no body provided
    if body is None:
        body = FilePrintRequest()

    # Get the library file
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    lib_file = result.scalar_one_or_none()

    if not lib_file:
        raise HTTPException(status_code=404, detail="File not found")

    # Validate file is sliced
    if not is_sliced_file(lib_file.filename):
        raise HTTPException(
            status_code=400,
            detail="Not a sliced file. Only .gcode or .gcode.3mf files can be printed.",
        )

    # Get the full file path
    file_path = Path(app_settings.base_dir) / lib_file.file_path

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    # Get printer
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Check printer is connected
    if not printer_manager.is_connected(printer_id):
        raise HTTPException(status_code=400, detail="Printer is not connected")

    # Validate project exists before dispatching so a bogus ID yields 404, not a FK-constraint 500
    if body.project_id is not None:
        project_result = await db.execute(select(Project).where(Project.id == body.project_id))
        if not project_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Project not found")

    plate_name = body.plate_name
    if not plate_name and body.plate_id is not None:
        plate_name = f"Plate {body.plate_id}"

    dispatch_source_name = lib_file.filename
    if plate_name:
        dispatch_source_name = f"{lib_file.filename} • {plate_name}"

    # Swap-macro execution only applies to swap-enabled printers AND files
    # that don't already carry swap macros baked in by third-party tooling
    # (``swap_compatible`` → double-fire risk). Mute the fields in either
    # case before they propagate into dispatch options or queued copies.
    if not printer.swap_mode_enabled or getattr(lib_file, "swap_compatible", False):
        body.execute_swap_macros = False
        body.swap_macro_events = None

    # Print-Now quantity handling:
    # * quantity == 1 → direct dispatch (no queue items, fastest path).
    # * quantity > 1 → route ALL copies through the queue, so the whole
    #   batch is visible in one place and there's no split between an
    #   "invisible" primary running via background_dispatch and queued
    #   siblings. The scheduler picks them up one by one.
    qty = max(1, body.quantity or 1)

    if qty > 1:
        from backend.app.services.queue_batch import enqueue_batch_copies

        items, batch_id = await enqueue_batch_copies(
            db,
            printer_id=printer_id,
            count=qty,
            library_file_id=file_id,
            plate_id=body.plate_id,
            ams_mapping=body.ams_mapping,
            bed_levelling=body.bed_levelling,
            flow_cali=body.flow_cali,
            layer_inspect=body.layer_inspect,
            timelapse=body.timelapse,
            use_ams=body.use_ams,
            mesh_mode_fast_check=body.mesh_mode_fast_check,
            execute_swap_macros=body.execute_swap_macros,
            swap_macro_events=body.swap_macro_events,
            created_by_id=current_user.id if current_user else None,
            project_id=body.project_id,
        )
        return {
            "status": "queued",
            "printer_id": printer_id,
            "archive_id": None,
            "filename": lib_file.filename,
            "dispatch_job_id": None,
            "dispatch_position": None,
            "batch_id": batch_id,
            "queued_copies": len(items),
        }

    try:
        dispatch_result = await background_dispatch.dispatch_print_library_file(
            file_id=file_id,
            filename=dispatch_source_name,
            printer_id=printer_id,
            printer_name=printer.name,
            options=body.model_dump(exclude_none=True, exclude={"cleanup_library_after_dispatch"}),
            project_id=body.project_id,
            requested_by_user_id=current_user.id if current_user else None,
            requested_by_username=current_user.username if current_user else None,
            cleanup_library_after_dispatch=body.cleanup_library_after_dispatch,
        )
    except DispatchEnqueueRejected as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return {
        "status": "dispatched",
        "printer_id": printer_id,
        "archive_id": None,
        "filename": lib_file.filename,
        "dispatch_job_id": dispatch_result["dispatch_job_id"],
        "dispatch_position": dispatch_result["dispatch_position"],
        "batch_id": None,
        "queued_copies": 0,
    }


# ============ File Detail Endpoints ============


@router.get("/files/{file_id}", response_model=FileResponseSchema)
async def get_file(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get a file by ID with full details."""
    result = await db.execute(
        select(LibraryFile)
        .options(
            selectinload(LibraryFile.created_by),
            selectinload(LibraryFile.projects),
        )
        .where(LibraryFile.id == file_id)
    )
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    # Get folder name
    folder_name = None
    if file.folder_id:
        folder_result = await db.execute(select(LibraryFolder.name).where(LibraryFolder.id == file.folder_id))
        folder_name = folder_result.scalar()

    # Get duplicates
    duplicates = []
    duplicate_count = 0
    if file.file_hash:
        # Trashed siblings are excluded from the duplicates panel — they're
        # already deleted from the user's perspective.
        dup_result = await db.execute(
            select(LibraryFile, LibraryFolder.name)
            .outerjoin(LibraryFolder, LibraryFile.folder_id == LibraryFolder.id)
            .where(
                LibraryFile.file_hash == file.file_hash,
                LibraryFile.id != file.id,
                LibraryFile.deleted_at.is_(None),
            )
        )
        for dup_file, dup_folder_name in dup_result.all():
            duplicates.append(
                FileDuplicate(
                    id=dup_file.id,
                    filename=dup_file.filename,
                    folder_id=dup_file.folder_id,
                    folder_name=dup_folder_name,
                    created_at=dup_file.created_at,
                )
            )
        duplicate_count = len(duplicates)

    # Extract key metadata fields
    print_name = None
    print_time = None
    filament_grams = None
    sliced_for_model = None
    object_count = None
    if file.file_metadata:
        print_name = file.file_metadata.get("print_name")
        print_time = file.file_metadata.get("print_time_seconds")
        filament_grams = file.file_metadata.get("filament_used_grams")
        sliced_for_model = file.file_metadata.get("sliced_for_model")
        printable_objects = file.file_metadata.get("printable_objects")
        if isinstance(printable_objects, dict):
            object_count = len(printable_objects)

    return FileResponseSchema(
        id=file.id,
        folder_id=file.folder_id,
        folder_name=folder_name,
        projects=_project_refs(file.projects),
        filename=file.filename,
        file_path=file.file_path,
        file_type=file.file_type,
        file_size=file.file_size,
        file_hash=file.file_hash,
        thumbnail_path=file.thumbnail_path,
        metadata=file.file_metadata,
        last_printed_at=file.last_printed_at,
        notes=file.notes,
        duplicates=duplicates if duplicates else None,
        duplicate_count=duplicate_count,
        created_by_id=file.created_by_id,
        created_by_username=file.created_by.username if file.created_by else None,
        created_at=file.created_at,
        updated_at=file.updated_at,
        print_name=print_name,
        print_time_seconds=print_time,
        filament_used_grams=filament_grams,
        object_count=object_count,
        sliced_for_model=sliced_for_model,
        swap_compatible=file.swap_compatible,
        source_type=file.source_type,
        source_url=file.source_url,
    )


@router.put("/files/{file_id}", response_model=FileResponseSchema)
async def update_file(
    file_id: int,
    data: FileUpdate,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_UPDATE_ALL,
            Permission.LIBRARY_UPDATE_OWN,
        )
    ),
):
    """Update a file's metadata."""
    user, can_modify_all = auth_result

    result = await db.execute(
        select(LibraryFile).options(selectinload(LibraryFile.projects)).where(LibraryFile.id == file_id)
    )
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    # Ownership check
    if not can_modify_all:
        if file.created_by_id != user.id:
            raise HTTPException(status_code=403, detail="You can only update your own files")

    if data.filename is not None:
        # Validate filename doesn't contain path separators
        if "/" in data.filename or "\\" in data.filename:
            raise HTTPException(status_code=400, detail="Filename cannot contain path separators")
        file.filename = data.filename
        # Also update print_name in file_metadata so the display name matches
        if file.file_metadata and "print_name" in file.file_metadata:
            file.file_metadata = {**file.file_metadata, "print_name": data.filename}

    # m044: track whether the project list changed in this PUT so the
    # plan-sync at the end fires exactly once.
    projects_touched = False

    if data.folder_id is not None:
        if data.folder_id == 0:
            file.folder_id = None
            # Moving to root clears project links — root has no folder,
            # no project ownership.
            file.projects = []
            projects_touched = True
        else:
            # Verify folder exists; inherit its project list so moving a
            # file into a project-linked folder backfills the file→project
            # pivot, and moving it into an unlinked folder clears it
            # (replace semantics, matching the legacy single-project rule
            # generalised to lists — see plan §D.1).
            folder_result = await db.execute(
                select(LibraryFolder)
                .options(selectinload(LibraryFolder.projects))
                .where(LibraryFolder.id == data.folder_id)
            )
            target_folder = folder_result.scalar_one_or_none()
            if not target_folder:
                raise HTTPException(status_code=404, detail="Folder not found")
            file.folder_id = data.folder_id
            file.projects = list(target_folder.projects)
            projects_touched = True

    # Explicit project_ids override wins over folder-inherited list.
    if data.project_ids is not None:
        new_project_rows = await _resolve_projects_for_assign(db, data.project_ids)
        file.projects = new_project_rows
        projects_touched = True

    if data.notes is not None:
        file.notes = data.notes if data.notes else None

    # Keep print-plan rows aligned with this file's final project list
    # (m044: one plan row per (project, file) pair).
    if projects_touched:
        await sync_plan_for_file(
            db,
            library_file_id=file.id,
            project_ids=[p.id for p in file.projects],
            file_type=file.file_type,
        )

    await db.commit()

    # Return full response (reuse get_file logic — it does its own
    # selectinload, so we don't need a refresh here).
    return await get_file(file_id, db)


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_DELETE_ALL,
            Permission.LIBRARY_DELETE_OWN,
        )
    ),
):
    """Move a file to the trash (soft-delete, #1008).

    Bytes + thumbnail stay on disk so a user can restore the file from
    Settings → Library Trash. After the configured retention window
    (default 30 days) the background sweeper hard-deletes both the row
    and the bytes. External files bypass the trash entirely — their
    bytes live outside BamDude's control, so there's nothing to restore.
    Queue items keep referencing the trashed row; printing items block
    the soft-delete so we never yank the file mid-job.
    """
    user, can_modify_all = auth_result

    # Use the active() filter so a row already in the trash returns 404 — the
    # caller should be hitting the trash endpoints to manage it instead.
    result = await db.execute(LibraryFile.active().where(LibraryFile.id == file_id))
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    # Ownership check
    if not can_modify_all:
        if file.created_by_id != user.id:
            raise HTTPException(status_code=403, detail="You can only delete your own files")

    # Block delete if any queue item referencing this file is currently
    # printing — pulling the file out from under a live job is unsafe
    # regardless of the trash bin (the file is still on disk but we'd
    # break the audit trail).
    queue_items_result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.library_file_id == file.id))
    queue_items = list(queue_items_result.scalars().all())
    printing_blockers = [qi for qi in queue_items if qi.status == "printing"]
    if printing_blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "file_in_use",
                "message": "Cannot delete file: a queue item is currently printing. Wait for or stop the print first.",
                "queue_item_ids": [qi.id for qi in printing_blockers],
            },
        )

    if file.is_external:
        # External files bypass the trash — just drop the DB row + our thumbnail
        # + clean up dependents. The on-disk file is outside BamDude's control.
        try:
            abs_thumb_path = to_absolute_path(file.thumbnail_path)
            if abs_thumb_path and abs_thumb_path.exists():
                abs_thumb_path.unlink()
        except OSError as e:
            logger.warning("Failed to delete thumbnail from disk: %s", e)
        await db.execute(
            update(PrintArchive).where(PrintArchive.library_file_id == file.id).values(library_file_id=None)
        )
        for qi in queue_items:
            await db.delete(qi)
        await db.delete(file)
        await db.commit()
        return {"status": "success", "message": "File deleted", "trashed": False}

    # Managed file: soft-delete. Bytes + thumbnail + queue refs stay; the
    # sweeper cleans up after the retention window, restore reverses this.
    file.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "success", "message": "File moved to trash", "trashed": True}


# ============ File Content Endpoints ============


@router.get("/files/{file_id}/download")
async def download_file(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Download a file."""
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    abs_path = to_absolute_path(file.file_path)
    if not abs_path or not abs_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FastAPIFileResponse(
        str(abs_path),
        filename=file.filename,
        media_type="application/octet-stream",
    )


@router.post("/files/{file_id}/slicer-token")
async def create_library_slicer_token(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Create a short-lived download token for opening files in slicer applications.

    Slicer protocol handlers (bambustudioopen://, orcaslicer://) cannot send
    auth headers, so they use this token in the URL path instead.
    """
    from backend.app.core.auth import create_slicer_download_token

    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    file = result.scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    token = await create_slicer_download_token("library", file_id)
    return {"token": token}


@router.get("/files/{file_id}/dl/{token}/{filename}")
async def download_library_file_for_slicer(
    file_id: int,
    token: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """Download a library file using a slicer download token.

    Token-authenticated (no auth headers needed). The token is short-lived
    and single-use, created by POST /files/{file_id}/slicer-token.
    Filename is at the end of the URL so slicers can detect the file format.
    """
    from backend.app.core.auth import verify_slicer_download_token

    if not await verify_slicer_download_token(token, "library", file_id):
        raise HTTPException(status_code=403, detail="Invalid or expired download token")

    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    file = result.scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    abs_path = to_absolute_path(file.file_path)
    if not abs_path or not abs_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FastAPIFileResponse(
        str(abs_path),
        filename=file.filename,
        media_type="application/octet-stream",
    )


@router.get("/files/{file_id}/thumbnail")
async def get_thumbnail(file_id: int, db: AsyncSession = Depends(get_db)):
    """Get a file's thumbnail."""
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    abs_thumb_path = to_absolute_path(file.thumbnail_path)
    if not abs_thumb_path or not abs_thumb_path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    # Detect media type from extension
    thumb_ext = abs_thumb_path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_types.get(thumb_ext, "image/png")

    return FastAPIFileResponse(str(abs_thumb_path), media_type=media_type)


@router.get("/files/{file_id}/gcode")
async def get_gcode(
    file_id: int,
    plate_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get gcode for a file (for preview).

    For multi-plate sliced 3MFs the caller passes ``plate_id`` to fetch
    a specific plate's gcode (``Metadata/plate_{N}.gcode``). Without
    ``plate_id`` we fall back to the first gcode entry — preserves the
    original single-plate behaviour for callers (e.g. printer dispatch)
    that don't know about the plates UI.
    """
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    abs_path = to_absolute_path(file.file_path)
    if not abs_path or not abs_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    # Branch by container, not by primary file_type — sliced 3MFs collapse
    # to file_type='gcode' (m035) but are still zip containers and need
    # the embedded-gcode extraction path. Filename suffix is the only
    # cheap way to tell raw .gcode from .gcode.3mf without opening the
    # bytes (and the bytes can be huge).
    is_3mf_container = file.filename.lower().endswith(".3mf")
    if file.file_type == "gcode" and not is_3mf_container:
        return FastAPIFileResponse(str(abs_path), media_type="text/plain")
    elif is_3mf_container:
        try:
            with zipfile.ZipFile(str(abs_path), "r") as zf:
                gcode_files = [n for n in zf.namelist() if n.endswith(".gcode")]
                if not gcode_files:
                    raise HTTPException(status_code=404, detail="No gcode found in 3MF file")

                target_name: str | None = None
                if plate_id is not None:
                    expected_suffix = f"plate_{plate_id}.gcode"
                    target_name = next(
                        (n for n in gcode_files if n.lower().endswith(expected_suffix)),
                        None,
                    )
                    if target_name is None:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Plate {plate_id} gcode not found in 3MF",
                        )
                else:
                    target_name = gcode_files[0]

                gcode_content = zf.read(target_name)
                from fastapi.responses import Response

                return Response(content=gcode_content, media_type="text/plain")
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid 3MF file")
    else:
        raise HTTPException(status_code=400, detail="Unsupported file type")


# ============ Bulk Operations ============


@router.post("/files/move")
async def move_files(
    data: FileMoveRequest,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_UPDATE_ALL,
            Permission.LIBRARY_UPDATE_OWN,
        )
    ),
):
    """Move multiple files to a folder.

    Cross-boundary moves (managed ↔ external, or external ↔ external)
    physically relocate the bytes — see ``_move_file_bytes``. Same-boundary
    moves stay DB-only because the file's on-disk location doesn't depend on
    which managed folder owns it.

    Files not owned by the user are skipped (unless user has ``*_all``
    permission). Each skip carries a structured reason so the UI can surface
    "5 of 10 files were skipped: 3 had filename collisions on the NAS, 2 are
    no longer on disk" rather than a blank "skipped: 5".
    """
    user, can_modify_all = auth_result

    # m044: capture the destination folder's M2M project list so moved
    # files inherit it (replace semantics — see plan §D.1). Empty list
    # when moving to root or to a folder that isn't linked to any project.
    target_folder: LibraryFolder | None = None
    target_project_rows: list[Project] = []
    if data.folder_id is not None:
        folder_result = await db.execute(
            select(LibraryFolder)
            .options(selectinload(LibraryFolder.projects))
            .where(LibraryFolder.id == data.folder_id)
        )
        target_folder = folder_result.scalar_one_or_none()
        if not target_folder:
            raise HTTPException(status_code=404, detail="Folder not found")
        if target_folder.is_external and target_folder.external_readonly:
            raise HTTPException(status_code=403, detail="Cannot move files to a read-only external folder")
        target_project_rows = list(target_folder.projects)

    target_project_ids = [p.id for p in target_project_rows]
    target_is_external = target_folder is not None and target_folder.is_external

    moved = 0
    skipped = 0
    skipped_reasons: list[dict] = []

    for file_id in data.file_ids:
        result = await db.execute(
            select(LibraryFile)
            .options(
                selectinload(LibraryFile.folder),
                selectinload(LibraryFile.projects),
            )
            .where(LibraryFile.id == file_id)
        )
        file = result.scalar_one_or_none()
        if not file:
            continue

        # Ownership check
        if not can_modify_all and file.created_by_id != user.id:
            skipped += 1
            skipped_reasons.append({"file_id": file_id, "code": "not_owner", "reason": "not the file owner"})
            continue

        # No bytes need to move when both ends are managed (same-boundary).
        if not file.is_external and not target_is_external:
            file.folder_id = data.folder_id
            file.projects = list(target_project_rows)
            await sync_plan_for_file(
                db,
                library_file_id=file.id,
                project_ids=target_project_ids,
                file_type=file.file_type,
            )
            moved += 1
            continue

        # Block moves out of a read-only external mount. The user only has
        # read access to the source, and a move is semantically a delete on
        # the source — which a read-only mount can't fulfil. Without this
        # guard we'd succeed at copying to the target, fail to unlink the
        # source, and the same file would now exist in two places (with the
        # DB pointing at only one).
        if file.is_external and file.folder is not None and file.folder.external_readonly:
            skipped += 1
            skipped_reasons.append(
                {"file_id": file_id, "code": "source_readonly", "reason": "source is on a read-only external folder"}
            )
            continue

        # Otherwise relocate the bytes, then update the DB row to match.
        try:
            new_file_path = _move_file_bytes(file, target_folder)
        except _MoveSkip as e:
            skipped += 1
            skipped_reasons.append({"file_id": file_id, "code": e.code, "reason": e.reason})
            continue

        file.is_external = target_is_external
        file.folder_id = data.folder_id
        file.projects = list(target_project_rows)
        file.file_path = new_file_path
        # External rows historically carry ``file_hash=None`` (scan skips
        # hashing). When pulling an external file into managed storage,
        # compute the hash so dedup detection works for future uploads of the
        # same content.
        if not target_is_external and file.file_hash is None:
            try:
                abs_path = to_absolute_path(new_file_path)
                if abs_path:
                    file.file_hash = calculate_file_hash(abs_path)
            except OSError:
                pass  # leave hash null; dedup just won't match this row
        await sync_plan_for_file(
            db,
            library_file_id=file.id,
            project_ids=target_project_ids,
            file_type=file.file_type,
        )
        moved += 1

    await db.commit()

    return {
        "status": "success",
        "moved": moved,
        "skipped": skipped,
        "skipped_reasons": skipped_reasons,
    }


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete(
    data: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_DELETE_ALL,
            Permission.LIBRARY_DELETE_OWN,
        )
    ),
):
    """Delete multiple files and/or folders.

    Files not owned by the user are skipped (unless user has *_all permission).
    """
    user, can_modify_all = auth_result
    deleted_files = 0
    deleted_folders = 0
    skipped_files = 0

    # Delete files first
    for file_id in data.file_ids:
        result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
        file = result.scalar_one_or_none()
        if file:
            # Ownership check
            if not can_modify_all and file.created_by_id != user.id:
                skipped_files += 1
                continue

            # Skip files that are currently being printed by a queue item —
            # cascading the file delete now would orphan the live print row.
            # See single-file delete_file for the full reasoning.
            queue_items_result = await db.execute(
                select(PrintQueueItem).where(PrintQueueItem.library_file_id == file.id)
            )
            queue_items = list(queue_items_result.scalars().all())
            if any(qi.status == "printing" for qi in queue_items):
                logger.info("bulk_delete: skipping file %s — queue item currently printing", file.id)
                skipped_files += 1
                continue

            try:
                if not file.is_external:
                    abs_file_path = to_absolute_path(file.file_path)
                    if abs_file_path and abs_file_path.exists():
                        abs_file_path.unlink()
                abs_thumb_path = to_absolute_path(file.thumbnail_path)
                if abs_thumb_path and abs_thumb_path.exists():
                    abs_thumb_path.unlink()
            except OSError as e:
                logger.warning("Failed to delete file from disk: %s", e)

            # Archives keep the SET NULL behaviour; queue items cascade-delete.
            await db.execute(
                update(PrintArchive).where(PrintArchive.library_file_id == file.id).values(library_file_id=None)
            )
            for qi in queue_items:
                await db.delete(qi)

            await db.delete(file)
            deleted_files += 1

    # Delete folders (cascade will handle contents)
    # Note: Folders don't have ownership tracking currently, require *_all permission
    for folder_id in data.folder_ids:
        if not can_modify_all:
            # Users without *_all permission cannot delete folders
            continue

        result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))
        folder = result.scalar_one_or_none()
        if folder:
            # Count files that will be deleted
            file_count_result = await db.execute(
                select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == folder_id)
            )
            deleted_files += file_count_result.scalar() or 0
            await db.delete(folder)
            deleted_folders += 1

    await db.commit()

    return BulkDeleteResponse(deleted_files=deleted_files, deleted_folders=deleted_folders)


# ============ Stats Endpoint ============


@router.get("/stats")
async def get_library_stats(
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get library statistics."""
    # Total files
    total_files_result = await db.execute(select(func.count(LibraryFile.id)))
    total_files = total_files_result.scalar() or 0

    # Total folders
    total_folders_result = await db.execute(select(func.count(LibraryFolder.id)))
    total_folders = total_folders_result.scalar() or 0

    # Total size
    total_size_result = await db.execute(select(func.sum(LibraryFile.file_size)))
    total_size = total_size_result.scalar() or 0

    # Files by type
    type_result = await db.execute(
        select(LibraryFile.file_type, func.count(LibraryFile.id)).group_by(LibraryFile.file_type)
    )
    files_by_type = dict(type_result.all())

    # Disk space info
    library_dir = get_library_dir()
    try:
        disk_stat = shutil.disk_usage(library_dir)
        disk_free_bytes = disk_stat.free
        disk_total_bytes = disk_stat.total
        disk_used_bytes = disk_stat.used
    except OSError:
        disk_free_bytes = 0
        disk_total_bytes = 0
        disk_used_bytes = 0

    return {
        "total_files": total_files,
        "total_folders": total_folders,
        "total_size_bytes": total_size,
        "files_by_type": files_by_type,
        "disk_free_bytes": disk_free_bytes,
        "disk_total_bytes": disk_total_bytes,
        "disk_used_bytes": disk_used_bytes,
    }
