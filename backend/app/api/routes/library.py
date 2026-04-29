"""API routes for File Manager (Library) functionality."""

import base64
import binascii
import contextlib
import hashlib
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

from backend.app.core.auth import (
    require_ownership_permission,
    require_permission,
)
from backend.app.core.config import settings as app_settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile, LibraryFolder
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
    ZipExtractError,
    ZipExtractResponse,
    ZipExtractResult,
)
from backend.app.services.archive import ThreeMFParser
from backend.app.services.print_plan import ensure_plan_row, remove_plan_row, sync_plan_for_folder
from backend.app.services.stl_thumbnail import generate_stl_thumbnail
from backend.app.utils.threemf_tools import extract_nozzle_mapping_from_3mf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library"])


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

    # Get all folders with project and archive joins
    result = await db.execute(
        select(LibraryFolder, Project.name, PrintArchive.print_name)
        .outerjoin(Project, LibraryFolder.project_id == Project.id)
        .outerjoin(PrintArchive, LibraryFolder.archive_id == PrintArchive.id)
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

    for folder, project_name, archive_name in rows:
        folder_item = FolderTreeItem(
            id=folder.id,
            name=folder.name,
            parent_id=folder.parent_id,
            project_id=folder.project_id,
            archive_id=folder.archive_id,
            project_name=project_name,
            archive_name=archive_name,
            is_external=folder.is_external,
            external_path=folder.external_path,
            external_readonly=folder.external_readonly,
            file_count=file_counts.get(folder.id, 0),
            children=[],
        )
        folder_map[folder.id] = folder_item

    # Link children to parents
    for folder, _, _ in rows:
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
    """Get all folders linked to a specific project."""
    result = await db.execute(
        select(LibraryFolder, Project.name)
        .outerjoin(Project, LibraryFolder.project_id == Project.id)
        .where(LibraryFolder.project_id == project_id)
        .order_by(LibraryFolder.name)
    )
    rows = result.all()

    folders = []
    for folder, project_name in rows:
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
                project_id=folder.project_id,
                archive_id=folder.archive_id,
                project_name=project_name,
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
                project_id=folder.project_id,
                archive_id=folder.archive_id,
                project_name=None,
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

    # Verify project exists if specified
    project_name = None
    if data.project_id is not None:
        project_result = await db.execute(select(Project).where(Project.id == data.project_id))
        project = project_result.scalar_one_or_none()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        project_name = project.name

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
        project_id=data.project_id,
        archive_id=data.archive_id,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        project_id=folder.project_id,
        archive_id=folder.archive_id,
        project_name=project_name,
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
        select(LibraryFolder, Project.name, PrintArchive.print_name)
        .outerjoin(Project, LibraryFolder.project_id == Project.id)
        .outerjoin(PrintArchive, LibraryFolder.archive_id == PrintArchive.id)
        .where(LibraryFolder.id == folder_id)
    )
    row = result.one_or_none()

    if not row:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder, project_name, archive_name = row

    # Get file count
    file_count_result = await db.execute(select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == folder_id))
    file_count = file_count_result.scalar() or 0

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        project_id=folder.project_id,
        archive_id=folder.archive_id,
        project_name=project_name,
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
    result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))
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

    # Update project_id (0 to unlink) — cascades to files inside the folder
    # so that linking a folder to a project backfills the project_id column
    # on every child file, matching user expectation.
    if data.project_id is not None:
        if data.project_id == 0:
            folder.project_id = None
            new_file_project_id: int | None = None
        else:
            # Verify project exists
            project_result = await db.execute(select(Project).where(Project.id == data.project_id))
            if not project_result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="Project not found")
            folder.project_id = data.project_id
            new_file_project_id = data.project_id
        await db.execute(
            update(LibraryFile).where(LibraryFile.folder_id == folder_id).values(project_id=new_file_project_id)
        )
        # Mirror the project change into print-plan rows for this folder's files.
        await sync_plan_for_folder(db, folder_id=folder_id, new_project_id=new_file_project_id)

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
    await db.refresh(folder)

    # Get file count and names
    file_count_result = await db.execute(select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == folder_id))
    file_count = file_count_result.scalar() or 0

    # Get project and archive names
    project_name = None
    archive_name = None
    if folder.project_id:
        project_result = await db.execute(select(Project.name).where(Project.id == folder.project_id))
        project_name = project_result.scalar()
    if folder.archive_id:
        archive_result = await db.execute(select(PrintArchive.print_name).where(PrintArchive.id == folder.archive_id))
        archive_name = archive_result.scalar()

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        project_id=folder.project_id,
        archive_id=folder.archive_id,
        project_name=project_name,
        archive_name=archive_name,
        is_external=folder.is_external,
        external_path=folder.external_path,
        external_readonly=folder.external_readonly,
        external_show_hidden=folder.external_show_hidden,
        file_count=file_count,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
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
        project_id=None,
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

            file_type = ext[1:] if ext else "unknown"
            # For compound extensions, use the meaningful part
            if file_type in ("3mf",) and len(filepath.suffixes) >= 2:
                inner = filepath.suffixes[-2].lower()
                if inner == ".gcode":
                    file_type = "gcode.3mf"

            # Extract thumbnail for 3mf files
            thumbnail_path = None
            file_metadata = None
            if file_type == "3mf":
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

            # Extract gcode thumbnail
            if file_type == "gcode" and thumbnail_path is None:
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
    query = LibraryFile.active().options(selectinload(LibraryFile.created_by))

    if folder_id is not None:
        query = query.where(LibraryFile.folder_id == folder_id)
    elif project_id is not None:
        # Single join instead of one query per folder (avoids N+1 pattern)
        query = query.join(LibraryFolder, LibraryFile.folder_id == LibraryFolder.id)
        query = query.where(LibraryFolder.project_id == project_id)
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
            dup_result = await db.execute(
                select(LibraryFile.file_hash, func.count(LibraryFile.id))
                .where(LibraryFile.file_hash.in_(hashes))
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
                project_id=f.project_id,
                is_external=f.is_external,
                filename=f.filename,
                file_type=f.file_type,
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
                notes_count=notes_counts.get(f.id, 0),
            )
        )

    return file_list


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
        # Handle files without extension
        file_type = ext[1:] if ext else "unknown"

        # Verify folder exists if specified
        target_folder: LibraryFolder | None = None
        if folder_id is not None:
            folder_result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))
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

        # Check for duplicates
        dup_result = await db.execute(select(LibraryFile.id).where(LibraryFile.file_hash == file_hash).limit(1))
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
            file_size=len(content),
            file_hash=file_hash,
            thumbnail_path=to_relative_path(thumbnail_path) if thumbnail_path else None,
            file_metadata=metadata if metadata else None,
            created_by_id=current_user.id if current_user else None,
            swap_compatible=swap_compatible,
        )
        db.add(library_file)
        await db.commit()
        await db.refresh(library_file)

        return FileUploadResponse(
            id=library_file.id,
            filename=library_file.filename,
            file_type=library_file.file_type,
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

    # Verify target folder exists if specified
    if folder_id is not None:
        folder_result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))
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
                    file_type = ext[1:] if ext else "unknown"

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
                        file_size=len(file_content),
                        file_hash=file_hash,
                        thumbnail_path=to_relative_path(thumbnail_path) if thumbnail_path else None,
                        file_metadata=metadata if metadata else None,
                        created_by_id=current_user.id if current_user else None,
                    )
                    db.add(library_file)
                    await db.flush()
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

    # Get all requested files
    result = await db.execute(select(LibraryFile).where(LibraryFile.id.in_(request.file_ids)))
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
            # Inherit project_id from the library file so project stats count the
            # item correctly even when bulk-added from File Manager with no project
            # context passed from the UI.
            max_position += 1
            queue_item = PrintQueueItem(
                printer_id=None,  # Unassigned
                library_file_id=file_id,
                project_id=lib_file.project_id,
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
        return {"file_id": file_id, "filename": lib_file.filename, "plates": [], "is_multi_plate": False}

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


@router.get("/files/{file_id}/filament-requirements")
async def get_library_file_filament_requirements(
    file_id: int,
    plate_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get filament requirements from a library file.

    Parses the 3MF file to extract filament slot IDs, types, colors, and usage.
    This enables AMS slot assignment when printing from the file manager.

    Args:
        file_id: The library file ID
        plate_id: Optional plate index to get filaments for a specific plate
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
                                }
                            )

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
        select(LibraryFile).options(selectinload(LibraryFile.created_by)).where(LibraryFile.id == file_id)
    )
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    # Get folder name
    folder_name = None
    if file.folder_id:
        folder_result = await db.execute(select(LibraryFolder.name).where(LibraryFolder.id == file.folder_id))
        folder_name = folder_result.scalar()

    # Get project name
    project_name = None
    if file.project_id:
        project_result = await db.execute(select(Project.name).where(Project.id == file.project_id))
        project_name = project_result.scalar()

    # Get duplicates
    duplicates = []
    duplicate_count = 0
    if file.file_hash:
        dup_result = await db.execute(
            select(LibraryFile, LibraryFolder.name)
            .outerjoin(LibraryFolder, LibraryFile.folder_id == LibraryFolder.id)
            .where(LibraryFile.file_hash == file.file_hash, LibraryFile.id != file.id)
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
        project_id=file.project_id,
        project_name=project_name,
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

    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
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

    if data.folder_id is not None:
        if data.folder_id == 0:
            file.folder_id = None
            # Moving to root clears project — root has no folder, no project.
            # Explicit data.project_id below still wins if set.
            file.project_id = None
        else:
            # Verify folder exists and inherit its project_id so moving a
            # file into a project-linked folder fills the column, and moving
            # it into an unlinked folder clears it.
            folder_result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == data.folder_id))
            target_folder = folder_result.scalar_one_or_none()
            if not target_folder:
                raise HTTPException(status_code=404, detail="Folder not found")
            file.folder_id = data.folder_id
            file.project_id = target_folder.project_id

    if data.project_id is not None:
        if data.project_id == 0:
            file.project_id = None
        else:
            # Verify project exists
            project_result = await db.execute(select(Project).where(Project.id == data.project_id))
            if not project_result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="Project not found")
            file.project_id = data.project_id

    if data.notes is not None:
        file.notes = data.notes if data.notes else None

    # Keep the print-plan in sync with this file's final project attachment.
    # Runs whenever folder_id or project_id touched the row — no-op for other
    # field edits (filename / notes).
    if data.folder_id is not None or data.project_id is not None:
        if file.project_id is None:
            await remove_plan_row(db, library_file_id=file.id)
        else:
            await ensure_plan_row(
                db,
                library_file_id=file.id,
                project_id=file.project_id,
                file_type=file.file_type,
            )

    await db.commit()
    await db.refresh(file)

    # Return full response (reuse get_file logic)
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
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
):
    """Get gcode for a file (for preview)."""
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    abs_path = to_absolute_path(file.file_path)
    if not abs_path or not abs_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    if file.file_type == "gcode":
        return FastAPIFileResponse(str(abs_path), media_type="text/plain")
    elif file.file_type == "3mf":
        # Extract gcode from 3mf
        try:
            with zipfile.ZipFile(str(abs_path), "r") as zf:
                # Find gcode file
                gcode_files = [n for n in zf.namelist() if n.endswith(".gcode")]
                if not gcode_files:
                    raise HTTPException(status_code=404, detail="No gcode found in 3MF file")
                gcode_content = zf.read(gcode_files[0])
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

    # Verify folder exists if specified, and pick up its project_id so moved
    # files inherit the destination folder's project (or null when moving to
    # root / to a folder that isn't linked).
    target_folder: LibraryFolder | None = None
    target_project_id: int | None = None
    if data.folder_id is not None:
        folder_result = await db.execute(select(LibraryFolder).where(LibraryFolder.id == data.folder_id))
        target_folder = folder_result.scalar_one_or_none()
        if not target_folder:
            raise HTTPException(status_code=404, detail="Folder not found")
        if target_folder.is_external and target_folder.external_readonly:
            raise HTTPException(status_code=403, detail="Cannot move files to a read-only external folder")
        target_project_id = target_folder.project_id

    target_is_external = target_folder is not None and target_folder.is_external

    moved = 0
    skipped = 0
    skipped_reasons: list[dict] = []

    for file_id in data.file_ids:
        result = await db.execute(
            select(LibraryFile).options(selectinload(LibraryFile.folder)).where(LibraryFile.id == file_id)
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
            file.project_id = target_project_id
            if target_project_id is None:
                await remove_plan_row(db, library_file_id=file.id)
            else:
                await ensure_plan_row(
                    db,
                    library_file_id=file.id,
                    project_id=target_project_id,
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
        file.project_id = target_project_id
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
        if target_project_id is None:
            await remove_plan_row(db, library_file_id=file.id)
        else:
            await ensure_plan_row(
                db,
                library_file_id=file.id,
                project_id=target_project_id,
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
