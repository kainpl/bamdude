"""Flush-only deletion seams. Callers own the complete guards, commit and disk cleanup.

These literal child policies also run on SQLite, where FK actions are disabled.
No helper in this module takes a lock, commits, opens a session or touches disk.
"""

from dataclasses import dataclass

from sqlalchemy import delete, select, update

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile, LibraryFileTag, LibraryFolder
from backend.app.services import (
    library_file_photos,
    part_stock,
    queue_source_release,
    spool_usage_service,
    spoolman_usage_history,
)
from backend.app.services.product_sync import purge_file_product_links


@dataclass(frozen=True)
class ArchiveRemoval:
    id: int
    identity: str
    file_path: str | None


@dataclass(frozen=True)
class LibraryRemoval:
    id: int
    file_path: str | None
    thumbnail_path: str | None
    external: bool
    covers: tuple[str, ...] = ()


@dataclass(frozen=True)
class FolderRemoval:
    vp_ids: tuple[int, ...]


async def archive_rows(db, archive: PrintArchive) -> ArchiveRemoval:
    from backend.app.models.active_print_spoolman import ActivePrintSpoolman
    from backend.app.models.archive_part import PrintArchivePart
    from backend.app.models.print_completion_receipt import PrintCompletionReceipt
    from backend.app.models.printer import Printer
    from backend.app.services.print_usage_journal import delete_for_archive

    result = ArchiveRemoval(archive.id, archive.recovery_identity, archive.file_path)
    await queue_source_release.source_purged(
        db, archive_ids=[archive.id], reason=queue_source_release.REASON_ARCHIVE_DELETED
    )
    await spool_usage_service.detach_archives(db, [archive.id])
    await spoolman_usage_history.detach_archive(db, archive.id)
    await part_stock.detach_archive(db, archive.id)
    await delete_for_archive(db, archive.id)
    for model in (PrintArchivePart, PrintCompletionReceipt, ActivePrintSpoolman):
        await db.execute(delete(model).where(model.archive_id == archive.id))
    await db.execute(update(LibraryFolder).where(LibraryFolder.archive_id == archive.id).values(archive_id=None))
    await db.execute(
        update(Printer)
        .where(Printer.awaiting_plate_clear_archive_id == archive.id)
        .values(awaiting_plate_clear_archive_id=None)
    )
    await db.delete(archive)
    await db.flush()
    return result


async def library_rows(db, file: LibraryFile, *, reason=queue_source_release.REASON_FILE_DELETED) -> LibraryRemoval:
    from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta
    from backend.app.services.library_trash import forget_file_notes

    metadata = await db.scalar(
        select(LibraryFileMakerworldMeta).where(LibraryFileMakerworldMeta.library_file_id == file.id)
    )
    covers = tuple(path for path in (metadata.cover_path, metadata.variant_cover_path) if path) if metadata else ()
    result = LibraryRemoval(file.id, file.file_path, file.thumbnail_path, bool(file.is_external), covers)
    await db.execute(update(PrintArchive).where(PrintArchive.library_file_id == file.id).values(library_file_id=None))
    await queue_source_release.source_purged(db, library_file_ids=[file.id], reason=reason)
    await purge_file_product_links(db, [file.id])
    await library_file_photos.forget_files(db, [file.id])
    await forget_file_notes(db, [file.id])
    await db.execute(delete(LibraryFileTag).where(LibraryFileTag.file_id == file.id))
    await db.execute(delete(LibraryFileMakerworldMeta).where(LibraryFileMakerworldMeta.library_file_id == file.id))
    await db.delete(file)
    await db.flush()
    from backend.app.services.library_delete_cleanup import stage

    stage(db, result)
    return result


async def folder_rows(db, folder_ids):
    """Disposable folder links, after its files have all been trashed/detached."""
    from backend.app.models.library_scan import LibraryScanJob
    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.library_delete_cleanup import stage
    from backend.app.services.product_sync import purge_folder_product_links

    ids = sorted(set(folder_ids))
    if not ids:
        return
    await purge_folder_product_links(db, ids)
    await db.execute(delete(LibraryScanJob).where(LibraryScanJob.folder_id.in_(ids)))
    vp_ids = tuple(await db.scalars(select(VirtualPrinter.id).where(VirtualPrinter.target_folder_id.in_(ids))))
    await db.execute(
        update(VirtualPrinter).where(VirtualPrinter.target_folder_id.in_(ids)).values(target_folder_id=None)
    )
    if vp_ids:
        stage(db, FolderRemoval(vp_ids))
