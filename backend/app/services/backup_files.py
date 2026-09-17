"""Verified backup files and reversible restore, without replacing mount roots."""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import asynccontextmanager, closing
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)
MANIFEST = "backup-manifest.json"
QUEUE_SOURCES_DIR = "queue-sources"
LEGACY_QUEUE_SOURCES_DIR = "queue-spool"
_operation_lock = asyncio.Lock()


class BackupBusyError(RuntimeError):
    pass


@asynccontextmanager
async def exclusive_operation():
    if _operation_lock.locked():
        raise BackupBusyError("A backup or restore is already running")
    async with _operation_lock:
        yield


def directories(settings) -> dict[str, Path]:
    """One map for both directions, including custom archive/calibration roots."""
    base = Path(settings.base_dir)
    return {
        "archive": Path(settings.archive_dir),
        "library": Path(settings.library_dir),
        "virtual_printer": base / "virtual_printer",
        "plate_calibration": Path(settings.plate_calibration_dir),
        "icons": base / "icons",
        "projects": Path(settings.projects_dir),
        "products": Path(settings.products_dir),
        "certs": base / "certs",
    }


def _stat(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or path.is_junction():
        raise ValueError(f"Backup refuses links: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f"Backup refuses special files: {path}")
    return info


def _signature(info):
    # Python 3.12 on Windows reports different ctime semantics through lstat
    # and fstat (creation vs metadata-change time). Birthtime agrees on both;
    # size/mtime and file identity still detect writes and replacements.
    changed = info.st_birthtime_ns if os.name == "nt" else info.st_ctime_ns
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, changed


def inventory(root: Path, *, exclude: Path | None = None) -> dict[str, tuple]:
    """lstat every entry; scandir errors must propagate rather than omit files."""
    result = {}

    def walk(path):
        info = _stat(path)
        relative = path.relative_to(root).as_posix()
        result[relative] = (stat.S_ISDIR(info.st_mode), _signature(info))
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    child = path / entry.name
                    if child != exclude:
                        walk(child)

    walk(root)
    return result


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_file(source: Path, target: Path) -> None:
    """Stream, sync and verify bytes; detect source changes and path replacement."""
    before = _stat(source)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Expected a file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256()
    with source.open("rb") as src:
        if _signature(os.fstat(src.fileno())) != _signature(before):
            raise ValueError(f"Source changed before copying: {source}")
        with target.open("xb") as dst:
            while block := src.read(1024 * 1024):
                dst.write(block)
                checksum.update(block)
            dst.flush()
            os.fsync(dst.fileno())
        if _signature(os.fstat(src.fileno())) != _signature(before):
            raise ValueError(f"Source changed while copying: {source}")
    if _signature(_stat(source)) != _signature(before):
        raise ValueError(f"Source changed after copying: {source}")
    if target.stat().st_size != before.st_size or digest(target) != checksum.hexdigest():
        raise ValueError(f"Copied file failed verification: {target}")
    shutil.copystat(source, target, follow_symlinks=False)


def copy_tree(source: Path, target: Path) -> None:
    before = inventory(source)
    if not before["."][0]:
        raise ValueError(f"Expected a directory: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for name, (is_dir, _) in before.items():
        if name == ".":
            continue
        destination = target / name  # SEC-PATH-OK: names come from inventory's relative_to(source).
        if is_dir:
            destination.mkdir(parents=True, exist_ok=True)
        else:
            copy_file(source / name, destination)  # SEC-PATH-OK: inventory-relative path, links rejected.
    if inventory(source) != before:
        raise ValueError(f"Source directory changed while copying: {source}")


def stage_files(settings, data_dir: Path, staging: Path) -> None:
    for name, source in directories(settings).items():
        target = staging / name  # SEC-PATH-OK: name is from the fixed directories() map.
        if source.exists() or source.is_symlink():
            copy_tree(source, target)
        else:
            target.mkdir()  # Empty/absent managed directories must survive the ZIP.
    for name in (".mfa_encryption_key", ".install_id"):
        source = data_dir / name  # SEC-PATH-OK: fixed optional-file names, not ZIP or request input.
        if source.exists() or source.is_symlink():
            copy_file(source, staging / name)


def stage_zigbee_db(data_dir: Path, staging: Path) -> None:
    """Include the network key and WAL using SQLite's online snapshot API.

    No radio is normal. An existing but unreadable/corrupt database is a failed
    backup, never a successful archive that silently loses paired devices.
    """
    from backend.app.core.db_portable import _snapshot_sqlite, validate_sqlite_backup

    source = data_dir / "zigbee/zigbee.db"
    if not source.exists() and not source.is_symlink():
        return
    _stat(source)
    destination = staging / "zigbee/zigbee.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _snapshot_sqlite(source, destination)
    validate_sqlite_backup(destination)


def _queue_source_records(backup_db: Path) -> list[tuple[str, int, str]]:
    """Ready queue objects named by the already-exported portable database.

    The database snapshot is the boundary: a capture completed after it is not
    in this backup, and an object the snapshot still names must be present and
    match its recorded hash.  Reading SQLite directly is intentional: this is
    the portable file produced for the backup, whether the live server runs on
    SQLite or PostgreSQL.
    """
    # ``sqlite3.Connection`` as a context manager commits/rolls back but does
    # not close. Close explicitly: on Windows the temporary portable DB cannot
    # be removed while that handle survives a rejected backup/restore.
    with closing(sqlite3.connect(backup_db)) as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'queue_sources'").fetchone()
        if table is None:
            return []  # Backups made before m173 remain restorable.
        columns = {row[1] for row in db.execute("PRAGMA table_info(queue_sources)")}
        needed = {"sha256", "size_bytes", "relative_path", "format", "state"}
        if not needed <= columns:
            raise ValueError("Backup database has an incomplete queue_sources table")
        rows = db.execute(
            "SELECT sha256, size_bytes, relative_path, format FROM queue_sources WHERE state = 'ready'"
        ).fetchall()

    records: list[tuple[str, int, str]] = []
    roots: set[str] = set()
    for sha256, size_bytes, relative_path, fmt in rows:
        if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("Backup database has an invalid queue source hash")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("Backup database has an invalid queue source size")
        if fmt not in ("3mf", "gcode"):
            raise ValueError("Backup database has an invalid queue source format")
        expected = f"{QUEUE_SOURCES_DIR}/objects/{sha256[:2]}/{sha256}.{fmt}"
        legacy = f"{LEGACY_QUEUE_SOURCES_DIR}/objects/{sha256[:2]}/{sha256}.{fmt}"
        if relative_path not in (expected, legacy):
            raise ValueError("Backup database has a queue source outside queue-sources")
        roots.add(relative_path.split("/", 1)[0])
        records.append((relative_path, size_bytes, sha256))
    if len(roots) > 1:
        raise ValueError("Backup database mixes queue-source directory versions")
    return records


def _verify_queue_object(path: Path, *, size_bytes: int, sha256: str) -> None:
    if not path.is_file() or path.stat().st_size != size_bytes or digest(path) != sha256:
        raise ValueError(f"Queue source is missing or corrupt: {path}")


def stage_queue_spool(data_dir: Path, staging: Path, backup_db: Path) -> None:
    """Stage precisely the ready queue objects referenced by *backup_db*.

    ``staging/*.part`` belongs to a live capture, never a queued job.  It is
    excluded by construction, as are objects published after the database
    export.  A missing or corrupted referenced object makes the whole backup
    fail instead of producing a restore that can dispatch only part of its
    queue.
    """
    records = _queue_source_records(backup_db)
    root_name = records[0][0].split("/", 1)[0] if records else QUEUE_SOURCES_DIR
    (staging / root_name / "objects").mkdir(parents=True, exist_ok=True)
    for relative, size_bytes, sha256 in records:
        root_name = relative.split("/", 1)[0]
        source_root = data_dir / root_name  # SEC-PATH-OK: the containment reference, resolved+compared below
        source = data_dir / relative  # SEC-PATH-OK: exact validated queue object layout.
        target = staging / relative  # SEC-PATH-OK: exact validated queue object layout.
        try:
            source.resolve().relative_to(source_root.resolve())
        except ValueError as exc:
            raise ValueError("Queue source escapes its spool") from exc
        copy_file(source, target)
        _verify_queue_object(target, size_bytes=size_bytes, sha256=sha256)


def validate_staged_queue_spool(staging: Path, backup_db: Path) -> None:
    """Reject a restore whose ready queue rows and files do not agree."""
    records = _queue_source_records(backup_db)
    expected = {relative for relative, _, _ in records}
    actual = {
        f"{root_name}/{name}"
        for root_name in (QUEUE_SOURCES_DIR, LEGACY_QUEUE_SOURCES_DIR)
        if (root := staging / root_name).exists()
        for name, (is_dir, _) in inventory(root).items()
        if name != "." and not is_dir
    }
    if actual != expected:
        raise ValueError("Backup queue-sources does not match the database snapshot")
    for relative, size_bytes, sha256 in records:
        _verify_queue_object(staging / relative, size_bytes=size_bytes, sha256=sha256)


def _contents(staging: Path):
    files, dirs = {}, []
    for name, (is_dir, _) in inventory(staging).items():
        if name in (".", MANIFEST):
            continue
        if is_dir:
            dirs.append(name)
        else:
            path = staging / name  # SEC-PATH-OK: inventory-relative path inside private staging.
            files[name] = {"size": path.stat().st_size, "sha256": digest(path)}
    return files, sorted(dirs)


def write_manifest(staging: Path) -> None:
    files, dirs = _contents(staging)
    (staging / MANIFEST).write_text(
        json.dumps({"format": 1, "files": files, "directories": dirs}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def verify_manifest(staging: Path) -> None:
    path = staging / MANIFEST
    if not path.exists():
        return  # Older ZIPs remain supported; extraction still checks CRC and paths.
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise ValueError("Unsupported backup manifest format")
        files, dirs = _contents(staging)
        if files != manifest.get("files") or dirs != manifest.get("directories"):
            raise ValueError("Backup manifest does not match the files (missing, changed or extra content)")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup manifest is not readable") from exc


def _member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    parts = name.rstrip("/").split("/")
    if (
        path.is_absolute()
        or "\\" in name
        or any(
            part in ("", ".", "..")
            or any(c in part for c in ':<>"|?*\x00')
            or part.endswith((".", " "))
            or part.split(".")[0].upper() in reserved
            for part in parts
        )
    ):
        raise ValueError(f"Unsafe path in backup ZIP: {name!r}")
    return path


def extract_zip(stream, staging: Path) -> None:
    """Validate all entries first, then extract with CRC checking and no links."""
    with zipfile.ZipFile(stream) as archive:
        entries, seen = [], set()
        for item in archive.infolist():
            # orig_filename retains NULs/backslashes that ZipInfo normalizes.
            relative = _member_path(item.orig_filename)
            key = relative.as_posix().casefold()
            mode = stat.S_IFMT(item.external_attr >> 16)
            if key in seen or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"Duplicate or special entry in backup ZIP: {item.filename!r}")
            seen.add(key)
            dest = (
                staging / relative
            ).resolve()  # SEC-PATH-OK: validated relative ZIP path, containment checked below.
            if not dest.is_relative_to(staging.resolve()):
                raise ValueError("Backup ZIP path escapes staging")
            entries.append((item, dest))
        for item, dest in entries:
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, dest.open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                dest.chmod(((item.external_attr >> 16) & 0o777) or 0o600)
        verify_manifest(staging)


def write_zip(staging: Path, output: Path) -> None:
    from backend.app.core.db_portable import _staged_output

    with _staged_output(output) as temporary, zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in inventory(staging):
            if name != ".":
                archive.write(staging / name, name)  # SEC-PATH-OK: inventory-relative paths.


class _Swap:
    def __init__(self, source: Path | None, target: Path, directory: bool):
        self.source, self.directory = source, directory
        if target.is_symlink() or target.is_junction():
            raise ValueError(f"Restore refuses linked destination: {target}")
        self.target = target.resolve()
        self.created_root = directory and not target.exists()
        self.root = self.target if directory else self.target.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix=".bamdude-restore-", dir=self.root))
        self.undo: list[tuple[Path, Path]] = []
        self.recovery_needed = False

    def prepare(self):
        (self.work / "old").mkdir()
        incoming = self.work / "new"
        incoming.mkdir()
        if self.directory:
            inventory(self.target, exclude=self.work)  # Reject existing links before any moves/deletion.
            copy_tree(self.source, incoming)
        else:
            if self.target.exists() or self.target.is_symlink():
                if not stat.S_ISREG(_stat(self.target).st_mode):
                    raise ValueError(f"Expected a destination file: {self.target}")
            if self.source is not None:
                copy_file(self.source, incoming / self.target.name)
                if self.target.name in (".mfa_encryption_key", ".install_id", "zigbee.db"):
                    (incoming / self.target.name).chmod(0o600)

    def _move(self, source: Path, target: Path):
        os.replace(source, target)
        self.undo.append((target, source))

    def apply(self):
        if self.directory:
            inventory(self.target, exclude=self.work)
            current = [p for p in self.target.iterdir() if p != self.work]
        else:
            current = [self.target] if self.target.exists() else []
        for path in current:
            self._move(path, self.work / "old" / path.name)
        for path in (self.work / "new").iterdir():
            self._move(path, self.root / path.name)

    def rollback(self):
        for source, target in reversed(self.undo):
            try:
                if target.exists() or target.is_symlink():
                    raise OSError(f"Rollback destination was changed: {target}")
                os.replace(source, target)
            except OSError:
                self.recovery_needed = True
                logger.exception("Restore rollback failed; recovery files retained at %s", self.work)
                raise
        self.undo.clear()

    def cleanup(self):
        if self.recovery_needed:
            return
        # Only the private staging directory allocated above may be removed.
        if self.work.is_symlink() or self.work.is_junction() or self.work.resolve().parent != self.root:
            raise ValueError("Restore staging path changed; refusing cleanup")
        inventory(self.work)  # Refuse links inserted after preflight as well.
        shutil.rmtree(self.work)
        if self.created_root and not any(self.root.iterdir()):
            self.root.rmdir()


class FileRestore:
    """Prepare all destinations, retain old contents, and commit only after DB swap."""

    def __init__(self, staging: Path, settings, data_dir: Path):
        self.targets = [(staging / n, p, True) for n, p in directories(settings).items() if (staging / n).exists()]
        for directory in (QUEUE_SOURCES_DIR, LEGACY_QUEUE_SOURCES_DIR):
            if (staging / directory).exists():
                self.targets.append(
                    (staging / directory, data_dir / directory, True)  # SEC-PATH-OK: two module constants above
                )
        for name in (".mfa_encryption_key", ".install_id", "zigbee/zigbee.db"):
            if (staging / name).exists():
                self.targets.append((staging / name, data_dir / name, False))  # SEC-PATH-OK: fixed allowlist above.
        if (staging / "zigbee/zigbee.db").exists():
            # Zigbee is stopped before prepare/apply. Retain old sidecars for
            # rollback, but never put them beside the restored standalone DB.
            for name in ("zigbee/zigbee.db-wal", "zigbee/zigbee.db-shm"):
                self.targets.append((None, data_dir / name, False))  # SEC-PATH-OK: fixed zigpy sidecar names.
        resolved = [target.resolve() for _, target, _ in self.targets]
        for i, path in enumerate(resolved):
            for other in resolved[i + 1 :]:
                if path == other or path.is_relative_to(other) or other.is_relative_to(path):
                    raise ValueError(f"Overlapping restore destinations: {path} and {other}")
        self.swaps: list[_Swap] = []
        self.committed = False

    def prepare(self):
        for source, target, directory in self.targets:
            swap = _Swap(source, target, directory)
            self.swaps.append(swap)  # Own staging before any copy can fail.
            swap.prepare()

    def apply(self):
        for swap in self.swaps:
            swap.apply()

    def finish(self):
        """Roll back unless DB replacement completed. Never discard failed recovery."""
        failures = []
        if not self.committed:
            for swap in reversed(self.swaps):
                try:
                    swap.rollback()
                except OSError as exc:
                    failures.append(str(exc))
        for swap in self.swaps:
            try:
                swap.cleanup()
            except (OSError, ValueError):
                # Cleanup cannot change active data; leave the old copy and name it.
                logger.exception("Could not remove restore staging at %s", swap.work)
        if failures:
            raise RuntimeError("File rollback needs recovery; retained copies are named in server logs")
