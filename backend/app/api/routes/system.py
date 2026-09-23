"""System information API routes."""

import asyncio
import os
import platform
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.config import APP_VERSION, settings
from backend.app.core.database import get_db
from backend.app.core.db_dialect import is_postgres
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.models.project import Project
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.spool import Spool
from backend.app.models.user import User
from backend.app.schemas.system import DbHealth
from backend.app.services import db_health
from backend.app.services.log_health import ScanResult, scan_logs
from backend.app.services.log_reader import collect_sensitive_strings
from backend.app.services.preview_runtime import get_preview_health
from backend.app.services.printer_manager import printer_manager

router = APIRouter(prefix="/system", tags=["system"])

STORAGE_USAGE_CACHE_SECONDS = 300
_storage_usage_cache: dict | None = None
_storage_usage_cache_ts: float | None = None
_storage_usage_lock = asyncio.Lock()


def get_directory_size(path: Path) -> int:
    """Calculate total size of a directory in bytes."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except (PermissionError, OSError):
        pass  # Return partial total if directory traversal is interrupted
    return total


def format_bytes(bytes_value: int) -> str:
    """Format bytes to human-readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_value < 1024:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024
    return f"{bytes_value:.1f} PB"


def format_uptime(seconds: float) -> str:
    """Format uptime in seconds to human-readable string."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")

    return " ".join(parts) if parts else "< 1m"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _get_database_paths() -> list[Path]:
    # ⚠️ Current name FIRST. Without it the storage breakdown omitted the
    # database entirely on every install since the rename — the largest file in
    # DATA_DIR, missing from the page that exists to say where the space went.
    # The legacy two stay so an install that has not been through the startup
    # rename still shows its file.
    candidates = [
        settings.base_dir / "bamdude.db",
        settings.base_dir / "bambuddy.db",
        settings.base_dir / "bambutrack.db",
    ]
    return [path for path in candidates if path.exists()]


def _get_database_items() -> list[dict]:
    items: list[dict] = []
    embedded_pg_dir = _get_embedded_pg_dir()
    if embedded_pg_dir is not None and embedded_pg_dir.exists():
        # One entry for the whole cluster: a PostgreSQL data directory is
        # thousands of files, and listing them would bury the page it is meant
        # to explain.
        size = get_directory_size(embedded_pg_dir)
        items.append(
            {
                "name": embedded_pg_dir.name,
                "path": str(embedded_pg_dir),
                "bytes": size,
                "formatted": format_bytes(size),
            }
        )
    for path in _get_database_paths():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "bytes": size,
                "formatted": format_bytes(size),
            }
        )
    items.sort(key=lambda item: item["bytes"], reverse=True)
    return items


def _get_embedded_pg_dir() -> Path | None:
    """``DATA_DIR/postgres`` when BamDude runs the bundled server.

    The cluster is the largest thing in DATA_DIR on such an install, and the
    storage breakdown — the page whose whole job is «where did the space go» —
    listed nothing for it, because the classifier only knew about SQLite files.

    An external server's files live on another machine, so there is nothing of
    ours to count and this returns None.
    """
    data_dir = getattr(settings, "embedded_pg_data_dir", None)
    if not settings.embedded_postgres or data_dir is None:
        return None
    return Path(data_dir).parent


def _get_app_dir() -> Path:
    return settings.static_dir.parent


def _get_data_dirs() -> list[Path]:
    return [
        settings.archive_dir,
        settings.library_dir,
        settings.projects_dir,
        settings.products_dir,
        settings.log_dir,
        settings.plate_calibration_dir,
        settings.base_dir / "virtual_printer",
        settings.base_dir / "firmware",
    ]


def _is_system_path(path: Path) -> bool:
    app_dir = _get_app_dir()
    if not _is_under(path, app_dir):
        return False
    return all(not _is_under(path, data_dir) for data_dir in _get_data_dirs())


def _get_storage_rules() -> list[tuple[str, str, Callable]]:
    base_dir = settings.base_dir
    archive_dir = settings.archive_dir
    library_dir = settings.library_dir
    virtual_printer_dir = base_dir / "virtual_printer"
    upload_dir = virtual_printer_dir / "uploads"

    db_paths = set(_get_database_paths())
    embedded_pg_dir = _get_embedded_pg_dir()

    return [
        (
            "database",
            "Database",
            lambda path: path in db_paths or (embedded_pg_dir is not None and _is_under(path, embedded_pg_dir)),
        ),
        (
            "library_thumbnails",
            "Library Thumbnails",
            lambda path: _is_under(path, library_dir / "thumbnails"),
        ),
        (
            "library_files",
            "Library Files",
            lambda path: _is_under(path, library_dir / "files"),
        ),
        (
            "library_other",
            "Library Other",
            lambda path: _is_under(path, library_dir),
        ),
        (
            "archive_timelapses",
            "Timelapses",
            lambda path: _is_under(path, archive_dir) and "timelapse" in path.name.lower(),
        ),
        (
            "archive_thumbnails",
            "Thumbnails",
            lambda path: _is_under(path, archive_dir) and path.name.lower().startswith("thumbnail"),
        ),
        (
            "archive_files",
            "Archives",
            lambda path: _is_under(path, archive_dir),
        ),
        (
            "virtual_printer_upload_cache",
            "Virtual Printer Upload Cache",
            lambda path: _is_under(path, upload_dir / "cache"),
        ),
        (
            "virtual_printer_uploads",
            "Virtual Printer Uploads",
            lambda path: _is_under(path, upload_dir),
        ),
        (
            "virtual_printer_certs",
            "Virtual Printer Certs",
            lambda path: _is_under(path, virtual_printer_dir / "certs"),
        ),
        (
            "virtual_printer_other",
            "Virtual Printer Other",
            lambda path: _is_under(path, virtual_printer_dir),
        ),
        (
            "attachments",
            "Attachments",
            lambda path: _is_under(path, settings.projects_dir) or _is_under(path, settings.products_dir),
        ),
        (
            "downloads",
            "Downloads",
            lambda path: _is_under(path, base_dir / "firmware"),
        ),
        (
            "plate_calibration",
            "Plate Calibration",
            lambda path: _is_under(path, settings.plate_calibration_dir),
        ),
        (
            "logs",
            "Logs",
            lambda path: _is_under(path, settings.log_dir),
        ),
    ]


def _classify_file(path: Path, rules: list[tuple[str, str, Callable]]) -> tuple[str, str]:
    for key, label, matcher in rules:
        try:
            if matcher(path):
                return key, label
        except OSError:
            continue
    return "other_data", "Other"


def _format_percentage(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((part / total) * 100, 2)


def _get_other_bucket(path: Path, base_dir: Path) -> str:
    try:
        relative = path.resolve().relative_to(base_dir.resolve())
    except ValueError:
        return path.parent.name or path.name

    parts = relative.parts
    return parts[0] if parts else path.name


def _walk_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    stack = [root for root in roots if root.exists()]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return files


def _scan_storage_usage() -> dict:
    base_dir = settings.base_dir
    rules = _get_storage_rules()

    roots = _get_data_dirs()

    seen_roots = set()
    unique_roots = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in seen_roots:
            seen_roots.add(resolved)
            unique_roots.append(root)

    total_bytes = 0
    error_count = 0
    category_sizes: dict[str, dict] = {}
    other_breakdown: dict[tuple[str, str], int] = {}
    database_items = _get_database_items()

    files = _walk_files(unique_roots)
    for file_path in files:
        try:
            size = file_path.stat().st_size
        except OSError:
            error_count += 1
            continue

        total_bytes += size

        key, label = _classify_file(file_path, rules)
        if key not in category_sizes:
            category_sizes[key] = {"key": key, "label": label, "bytes": 0}
        category_sizes[key]["bytes"] += size

        if key == "other_data":
            bucket = _get_other_bucket(file_path, base_dir)
            kind = "system" if _is_system_path(file_path) else "data"
            other_breakdown[(bucket, kind)] = other_breakdown.get((bucket, kind), 0) + size

    for item in database_items:
        total_bytes += item["bytes"]
        key = "database"
        label = "Database"
        if key not in category_sizes:
            category_sizes[key] = {"key": key, "label": label, "bytes": 0}
        category_sizes[key]["bytes"] += item["bytes"]

    categories = []
    for item in category_sizes.values():
        bytes_value = item["bytes"]
        categories.append(
            {
                "key": item["key"],
                "label": item["label"],
                "bytes": bytes_value,
                "formatted": format_bytes(bytes_value),
                "percent_of_total": _format_percentage(bytes_value, total_bytes),
            }
        )

    categories.sort(key=lambda entry: entry["bytes"], reverse=True)

    other_items = []
    for (bucket, kind), size in other_breakdown.items():
        other_items.append(
            {
                "bucket": bucket,
                "label": bucket,
                "kind": kind,
                "deletable": kind != "system",
                "bytes": size,
                "formatted": format_bytes(size),
                "percent_of_total": _format_percentage(size, total_bytes),
            }
        )

    other_items.sort(key=lambda entry: entry["bytes"], reverse=True)

    return {
        "roots": [str(root) for root in unique_roots],
        "total_bytes": total_bytes,
        "total_formatted": format_bytes(total_bytes),
        "categories": categories,
        "other_breakdown": other_items,
        "scan_errors": error_count,
    }


async def _get_storage_usage_cached(refresh: bool, max_age_seconds: int) -> dict:
    global _storage_usage_cache
    global _storage_usage_cache_ts

    now = time.time()
    if not refresh and _storage_usage_cache and _storage_usage_cache_ts is not None:
        age = now - _storage_usage_cache_ts
        if age < max_age_seconds:
            return {
                **_storage_usage_cache,
                "cache": {
                    "hit": True,
                    "age_seconds": round(age, 2),
                    "max_age_seconds": max_age_seconds,
                },
            }

    async with _storage_usage_lock:
        now = time.time()
        if not refresh and _storage_usage_cache and _storage_usage_cache_ts is not None:
            age = now - _storage_usage_cache_ts
            if age < max_age_seconds:
                return {
                    **_storage_usage_cache,
                    "cache": {
                        "hit": True,
                        "age_seconds": round(age, 2),
                        "max_age_seconds": max_age_seconds,
                    },
                }

        snapshot = await asyncio.to_thread(_scan_storage_usage)
        _storage_usage_cache = {
            **snapshot,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        _storage_usage_cache_ts = time.time()
        return {
            **_storage_usage_cache,
            "cache": {
                "hit": False,
                "age_seconds": 0,
                "max_age_seconds": max_age_seconds,
            },
        }


@router.get("/info")
async def get_system_info(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SYSTEM_READ),
):
    """Get comprehensive system information."""

    # Database stats
    archive_count = await db.scalar(select(func.count(PrintArchive.id)))
    printer_count = await db.scalar(select(func.count(Printer.id)))
    # Count only active spools — archived ones aren't shown in the inventory
    # UI either, so the Information-page "Filaments" stat should match what
    # the user actually sees in the spool list.
    spool_count = await db.scalar(select(func.count(Spool.id)).where(Spool.archived_at.is_(None)))
    project_count = await db.scalar(select(func.count(Project.id)))
    smart_plug_count = await db.scalar(select(func.count(SmartPlug.id)))

    # Archive stats by status
    completed_count = await db.scalar(select(func.count(PrintArchive.id)).where(PrintArchive.status == "completed"))
    failed_count = await db.scalar(select(func.count(PrintArchive.id)).where(PrintArchive.status == "failed"))
    printing_count = await db.scalar(select(func.count(PrintArchive.id)).where(PrintArchive.status == "printing"))

    # Total print time
    total_print_time = (
        await db.scalar(
            select(func.sum(PrintArchive.print_time_seconds)).where(PrintArchive.print_time_seconds.isnot(None))
        )
        or 0
    )

    # Total filament used
    total_filament = (
        await db.scalar(
            select(func.sum(PrintArchive.filament_used_grams)).where(PrintArchive.filament_used_grams.isnot(None))
        )
        or 0
    )

    # Connected printers
    connected_printers = []
    for printer_id, client in printer_manager._clients.items():
        state = client.state
        if state and state.connected:
            # Get printer name and model from database
            result = await db.execute(select(Printer.name, Printer.model).where(Printer.id == printer_id))
            row = result.first()
            name = row[0] if row else f"Printer {printer_id}"
            model = row[1] if row else "unknown"
            connected_printers.append(
                {
                    "id": printer_id,
                    "name": name,
                    "state": state.state,
                    "model": model,
                }
            )

    # Storage info
    archive_dir = settings.archive_dir
    archive_size = get_directory_size(archive_dir) if archive_dir.exists() else 0

    # Database size, per backend. Statting ``bamdude.db`` is right only on
    # SQLite; on PostgreSQL that file does not exist, so this field reported
    # **0** on every PostgreSQL install — including the bundled one, whose
    # cluster is sitting in DATA_DIR the whole time. PostgreSQL can answer for
    # itself (``pg_database_size``), so ask it.
    #
    # Best-effort: the System page must render even if the probe cannot run.
    try:
        db_size = await db_health.probe_size_bytes(db) or 0
    except Exception:  # noqa: BLE001
        db_size = 0

    # Disk usage
    disk = psutil.disk_usage(str(settings.base_dir))

    # System info
    memory = psutil.virtual_memory()
    # PID 1's create_time is the right uptime anchor in containerised installs
    # (Docker, LXC) — psutil.boot_time() reads /proc/stat:btime which on a
    # shared-kernel container is the host's boot time, not the container's
    # (#1690). On bare metal / VMs PID 1 is the host init, which starts at
    # boot, so the value matches psutil.boot_time() within a sub-second.
    # Emit tz-aware UTC so isoformat() carries a "+00:00" marker. A naive
    # datetime serialises with no marker, and the frontend's parseUTCDate()
    # then appends 'Z' and converts UTC → local, applying the local offset a
    # second time — the #1690 follow-up double-offset the reporter saw.
    try:
        boot_time = datetime.fromtimestamp(psutil.Process(1).create_time(), tz=timezone.utc)
    except (psutil.Error, OSError):
        boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    uptime_seconds = (datetime.now(timezone.utc) - boot_time).total_seconds()

    # PID 1 describes the container or host. The current process is the
    # BamDude service on native installs, so it answers the separate question
    # "did BamDude restart?" without changing the established system fields.
    try:
        app_started_at = datetime.fromtimestamp(psutil.Process().create_time(), tz=timezone.utc)
        app_uptime_seconds: float | None = max(0.0, time.time() - app_started_at.timestamp())
    except (psutil.Error, OSError):
        app_started_at = None
        app_uptime_seconds = None

    # Python and system info
    import sys

    # Database engine + version
    if is_postgres():
        engine_name = "PostgreSQL"
        # SHOW server_version returns a single clean value (e.g. "17.2")
        db_version_row = await db.execute(sa_text("SHOW server_version"))
    else:
        engine_name = "SQLite"
        db_version_row = await db.execute(sa_text("SELECT sqlite_version()"))
    db_version = db_version_row.scalar() or ""

    return {
        "app": {
            "version": APP_VERSION,
            "base_dir": str(settings.base_dir),
            "archive_dir": str(archive_dir),
            "started_at": app_started_at.isoformat() if app_started_at else None,
            "uptime_seconds": app_uptime_seconds,
            "uptime_formatted": format_uptime(app_uptime_seconds) if app_uptime_seconds is not None else None,
        },
        "preview": get_preview_health().model_dump(),
        "database": {
            "engine": engine_name,
            "version": db_version,
            "archives": archive_count,
            "archives_completed": completed_count,
            "archives_failed": failed_count,
            "archives_printing": printing_count,
            "printers": printer_count,
            "filaments": spool_count,
            "projects": project_count,
            "smart_plugs": smart_plug_count,
            "total_print_time_seconds": total_print_time,
            "total_print_time_formatted": format_uptime(total_print_time),
            "total_filament_grams": round(total_filament, 1),
            "total_filament_kg": round(total_filament / 1000, 2),
        },
        "printers": {
            "total": printer_count,
            "connected": len(connected_printers),
            "connected_list": connected_printers,
        },
        "storage": {
            "archive_size_bytes": archive_size,
            "archive_size_formatted": format_bytes(archive_size),
            "database_size_bytes": db_size,
            "database_size_formatted": format_bytes(db_size),
            "disk_total_bytes": disk.total,
            "disk_total_formatted": format_bytes(disk.total),
            "disk_used_bytes": disk.used,
            "disk_used_formatted": format_bytes(disk.used),
            "disk_free_bytes": disk.free,
            "disk_free_formatted": format_bytes(disk.free),
            "disk_percent_used": disk.percent,
        },
        "system": {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "architecture": platform.machine(),
            "hostname": platform.node(),
            "python_version": sys.version.split()[0],
            "uptime_seconds": uptime_seconds,
            "uptime_formatted": format_uptime(uptime_seconds),
            "boot_time": boot_time.isoformat(),
        },
        "memory": {
            "total_bytes": memory.total,
            "total_formatted": format_bytes(memory.total),
            "available_bytes": memory.available,
            "available_formatted": format_bytes(memory.available),
            "used_bytes": memory.used,
            "used_formatted": format_bytes(memory.used),
            "percent_used": memory.percent,
        },
        "cpu": {
            "count": psutil.cpu_count(),
            "count_logical": psutil.cpu_count(logical=True),
            "percent": psutil.cpu_percent(interval=0.1),
        },
    }


@router.get("/storage-usage")
async def get_storage_usage(
    refresh: bool = False,
    max_age_seconds: int = STORAGE_USAGE_CACHE_SECONDS,
    _: User | None = RequirePermission(Permission.SYSTEM_READ),
):
    """Get storage usage breakdown for BamDude data directories."""
    max_age_seconds = max(0, min(max_age_seconds, 3600))
    return await _get_storage_usage_cached(refresh=refresh, max_age_seconds=max_age_seconds)


@router.get("/health", response_model=ScanResult)
async def get_system_health(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SYSTEM_READ),
):
    """Scan the recent application log against the known-issue catalog.

    Powers the self-service triage surfaces (System page + bug reporter).
    Sample lines are sanitized before they leave the process.
    """
    sensitive_strings = await collect_sensitive_strings(db)
    return await asyncio.to_thread(scan_logs, sensitive_strings=sensitive_strings)


@router.get("/database", response_model=DbHealth)
async def get_database_health(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SYSTEM_READ),
):
    """What the database can say about itself, on either backend.

    Separate from ``/system/info`` because that one is polled every 30 s by a
    page that also wants disk and CPU, while this is heavier, dialect-branched
    and interesting at a slower cadence. Reuses ``SYSTEM_READ``, so it needs no
    new permission, no API-key scope edit and no migration.
    """
    return await db_health.collect(db)


@router.get("/db-pool")
async def get_db_pool(
    _: User | None = RequirePermission(Permission.SYSTEM_READ),
):
    """Live database connection-pool gauges for large-farm diagnostics (#2572).

    Reports the resolved pool configuration plus current checked-out /
    checked-in / overflow counts. Deliberately takes no DB session — reading the
    pool's own counters must not itself consume a connection, so this stays
    truthful even when the pool is saturated. On a healthy install ``checked_out``
    sits well below ``config.pool_size + config.max_overflow``; sustained
    saturation points at connections held across slow I/O (see #2572).
    """
    from backend.app.core.database import get_pool_status

    return get_pool_status()
