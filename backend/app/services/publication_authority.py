"""Carry one captured actor through preparation into an existing publication."""

from fastapi import HTTPException

from backend.app.services import domain_authority


def actor_for_writer(user=None, *, authority=None, receipt=None, consumption=None):
    if authority is not None:
        domain_authority.captured(authority)
        return authority
    if receipt is not None:
        if receipt.actor.capture is None:
            raise HTTPException(401, "Immutable authority is required")
        return receipt.actor.capture
    if consumption is not None and getattr(consumption, "access", None) is not None:
        capture = consumption.access.actor.capture
        if capture is None:
            raise HTTPException(401, "Immutable authority is required")
        return capture
    if consumption is not None and getattr(consumption, "capture", None) is not None:
        return consumption.capture
    if user is not None:
        capture = getattr(user, "_auth_capture", None)
        if capture is None:
            raise HTTPException(401, "Immutable authority is required")
        return capture
    # Only explicit existing server/observation callers reach this path.
    # Request routes always pass their authenticated scalar capture.
    return None


def publication_checks(authority, *, permissions=("queue:create",), printer_id=None, source_access=None):
    proof = None

    async def prepare(db):
        nonlocal proof
        proof, principal = await domain_authority.guard(db, authority, permissions=permissions, printer_id=printer_id)
        if source_access is not None:
            await source_access(db, principal)

    async def reread(db):
        principal = await domain_authority.fresh(
            db, authority, permissions=permissions, printer_id=printer_id, proof=proof
        )
        if source_access is not None:
            await source_access(db, principal)
        return principal

    return prepare, reread


async def current_creator(db, authority, fallback=None):
    """Attribute new work to the guarded actor, including a key's real owner."""
    if authority is None:
        return fallback
    principal = await domain_authority.fresh(db, authority)
    return principal.user if principal is not None else None


def original_source_access(authority, *, archive_id=None, library_file_id=None):
    async def check(db, principal):
        if authority is None:
            return
        if getattr(authority, "kind", None) == "telegram_chat":
            permission = "library:read" if library_file_id else "archives:read"
            await domain_authority.fresh(db, authority, permissions=(permission,))
        from backend.app.models.archive import PrintArchive
        from backend.app.models.library import LibraryFile
        from backend.app.services.queue_add import ensure_source_visible

        archive = await db.get(PrintArchive, archive_id, populate_existing=True) if archive_id else None
        library = await db.get(LibraryFile, library_file_id, populate_existing=True) if library_file_id else None
        if archive_id and (archive is None or archive.deleted_at is not None):
            raise HTTPException(404, "Archive not found")
        if library_file_id and (library is None or library.deleted_at is not None):
            raise HTTPException(404, "Library file not found")
        # API-key source visibility retains its existing adapter policy; its
        # current key/owner ceiling was checked by the shared validator.
        user = principal.user if principal is not None and principal.capture.kind == "session" else None
        ensure_source_visible(user, archive=archive, library_file=library)

    return check


def queue_item_access(items):
    """Recheck the existing queue-source visibility rule on current rows."""
    pins = tuple((item.id, item.recovery_identity, item.queue_source_id) for item in items)

    async def check(db, principal):
        if principal is None:
            return
        from backend.app.models.print_queue import PrintQueueItem

        for item_id, identity, source_id in pins:
            item = await db.get(PrintQueueItem, item_id, populate_existing=True)
            if item is None or (item.recovery_identity, item.queue_source_id) != (identity, source_id):
                raise HTTPException(404, "Queue item not found")
            if principal.capture.kind == "session":
                user = principal.user
                if not user.has_permission("queue:read_all") and item.created_by_id != user.id:
                    raise HTTPException(404, "Queue item not found")

    return check
