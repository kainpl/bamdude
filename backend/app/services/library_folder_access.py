"""Who sees which library folder, and who may put something into it (N56, upstream 6735e4556).

Spec: vault ``60-specs/n56-library-folder-ownership-spec.md``. The one rule of folders, as
``models/library.owner_scope_sees`` is the one rule of files; no route decides a folder's
visibility by itself.

A folder has an owner (``created_by_id``, the actor who made it) and may be ``shared``
with everyone. A caller who sees every library file (``sees_all``) sees every folder. A
``library:read_own`` caller sees:

- the folders they own,
- shared folders,
- the folders holding one of their live files (what the file rule shows them),
- and the folders above those, for navigation only.

They put files and subfolders only into their own folders, shared ones and the root.
Everything else answers 404, exactly like another user's file. The owner shares their
own folder; ``library:update_all`` shares any (owner decision 2026-11-27); an external
folder stays shared — its ownerless files are everyone's anyway.

Every function takes the ACTOR and ``sees_all`` from its door (spec §4): the library's
read routes from ``require_ownership_permission``; the upload, slice, assembly, import
and archive doors from :func:`request_actor`; Telegram from its chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.user import User


@dataclass(frozen=True)
class FolderActor:
    """Who acts on folders, and whether they see every library file."""

    user: User | None
    sees_all: bool


async def request_actor(request, db: AsyncSession, current_user: User | None) -> FolderActor:
    """The actor of a library WRITE door: the person or the API key's owner, with the same
    "sees all" every library name check uses (``core/auth.library_name_scope``)."""
    from backend.app.core.auth import acting_user, library_name_scope  # noqa: PLC0415

    actor = await acting_user(request, db, current_user)
    scope = await library_name_scope(request, db, actor)
    return FolderActor(actor, scope == "all")


def owns(folder: LibraryFolder, user: User | None) -> bool:
    return user is not None and folder.created_by_id is not None and folder.created_by_id == user.id


def can_write_folder(folder: LibraryFolder | None, actor: FolderActor) -> bool:
    """May the actor put files or subfolders into ``folder`` (None: the root, always)."""
    if folder is None:
        return True
    return actor.sees_all or owns(folder, actor.user) or bool(folder.shared)


def can_rename_folder(folder: LibraryFolder, user: User | None, *, modify_all: bool) -> bool:
    """Rename or move: ``library:update_all`` (``modify_all``), or the owner with ``update_own``."""
    return modify_all or owns(folder, user)


def can_share_folder(folder: LibraryFolder, user: User | None, *, modify_all: bool) -> bool:
    """Make shared or private (D3). Never an external folder: its ownerless files are
    everyone's by the file rule, so "private" would hide nothing."""
    if folder.is_external:
        return False
    return modify_all or owns(folder, user)


@dataclass
class FolderIndex:
    """Every folder, who owns the files in each, and which are bound to a product.

    ``file_owners`` counts trashed files too (a folder's files go to the trash with it,
    so they decide who may delete it); ``live_file_owners`` only what the file rule shows.
    The file facts are loaded only when the caller's answer needs them.
    """

    folders: dict[int, LibraryFolder] = field(default_factory=dict)
    children: dict[int | None, list[int]] = field(default_factory=dict)
    file_owners: dict[int, set[int | None]] = field(default_factory=dict)
    live_file_owners: dict[int, set[int | None]] = field(default_factory=dict)
    linked: set[int] = field(default_factory=set)

    def subtree(self, folder_id: int) -> list[int]:
        """``folder_id`` and every folder below it; a ``parent_id`` loop cannot hang."""
        out: list[int] = []
        seen: set[int] = set()
        stack = [folder_id]
        while stack:
            fid = stack.pop()
            if fid in seen:
                continue
            seen.add(fid)
            out.append(fid)
            stack.extend(self.children.get(fid, []))
        return out


async def load_folder_index(db: AsyncSession, *, with_files: bool = True) -> FolderIndex:
    from backend.app.models.product import product_folders  # noqa: PLC0415

    index = FolderIndex()
    for folder in (await db.execute(select(LibraryFolder))).scalars().all():
        index.folders[folder.id] = folder
        index.children.setdefault(folder.parent_id, []).append(folder.id)
    if not with_files:
        return index
    index.linked = set((await db.execute(select(product_folders.c.library_folder_id).distinct())).scalars().all())
    rows = await db.execute(
        select(LibraryFile.folder_id, LibraryFile.created_by_id, LibraryFile.deleted_at.is_(None))
        .where(LibraryFile.folder_id.isnot(None))
        .distinct()
    )
    for folder_id, owner_id, live in rows.all():
        index.file_owners.setdefault(folder_id, set()).add(owner_id)
        if live:
            index.live_file_owners.setdefault(folder_id, set()).add(owner_id)
    return index


def visible_folder_ids(index: FolderIndex, actor: FolderActor) -> set[int] | None:
    """The folders the actor sees (D1), or None for all of them."""
    if actor.sees_all:
        return None
    user_id = actor.user.id if actor.user is not None else None

    def holds_a_file_they_see(fid: int, folder: LibraryFolder) -> bool:
        # The file rule (``owner_scope_sees``): their own, or an ownerless one of an external folder.
        owners = index.live_file_owners.get(fid, set())
        return (user_id is not None and user_id in owners) or (folder.is_external and None in owners)

    reachable = {
        fid
        for fid, folder in index.folders.items()
        if owns(folder, actor.user) or folder.shared or holds_a_file_they_see(fid, folder)
    }
    visible = set(reachable)
    for fid in reachable:
        parent = index.folders[fid].parent_id
        while parent is not None and parent not in visible and parent in index.folders:
            visible.add(parent)
            parent = index.folders[parent].parent_id
    return visible


def folder_delete_blocker(
    index: FolderIndex, folder: LibraryFolder, user: User | None, *, delete_all: bool
) -> str | None:
    """Why the caller may NOT delete ``folder``, or None if they may (spec §5.6).

    ``library:delete_all`` deletes anything. With ``delete_own`` the owner deletes a folder
    when everything under it — folders and files, trashed ones too — is theirs, nothing is
    external and nothing is bound to a product. A folder without an owner keeps the old
    rule (#1781): only when it is truly empty. Needs the index WITH its file facts.

    ⚠️ "Empty" includes TRASHED files: a folder delete detaches them, and a file binned
    by someone else would leave the folder it can no longer be restored into.
    """
    if delete_all:
        return None
    if folder.is_external:
        return "External folders can only be deleted by users with library:delete_all"
    if any(fid in index.linked for fid in index.subtree(folder.id)):
        return "Folders linked to a product can only be deleted by users with library:delete_all"
    if folder.created_by_id is None:
        if index.children.get(folder.id):
            return "Only empty folders can be deleted without library:delete_all"
        if index.file_owners.get(folder.id):
            return "Only empty folders can be deleted without library:delete_all (the folder may contain trashed files)"
        return None
    if not owns(folder, user):
        return "Only the folder's owner can delete it without library:delete_all"
    for fid in index.subtree(folder.id):
        sub = index.folders[fid]
        if fid != folder.id and not owns(sub, user):
            return "The folder contains folders of other users; deleting it requires library:delete_all"
        if sub.is_external:
            return "The folder contains external folders; deleting it requires library:delete_all"
        if index.file_owners.get(fid, set()) - {user.id}:
            return (
                "The folder contains files of other users (the folder may contain trashed files); "
                "deleting it requires library:delete_all"
            )
    return None


async def get_visible_folder(
    db: AsyncSession, folder_id: int, actor: FolderActor, *, missing: str = "Folder not found"
) -> LibraryFolder:
    """The folder, or 404 (``missing``) when it does not exist or the actor cannot see it —
    one answer for both, as for another user's file."""
    folder = (await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))).scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail=missing)
    if not actor.sees_all:
        visible = visible_folder_ids(await load_folder_index(db), actor)
        if visible is not None and folder.id not in visible:
            raise HTTPException(status_code=404, detail=missing)
    return folder


async def get_writable_folder(
    db: AsyncSession, folder_id: int | None, actor: FolderActor, *, missing: str = "Folder not found"
) -> LibraryFolder | None:
    """The folder the actor puts something into (None: the root): 404 when unseen, 403
    when only seen."""
    if folder_id is None:
        return None
    folder = await get_visible_folder(db, folder_id, actor, missing=missing)
    if not can_write_folder(folder, actor):
        raise HTTPException(
            status_code=403,
            detail="You can only add to your own folders and folders shared with everyone",
        )
    return folder


async def writable_folder_named(
    db: AsyncSession,
    name: str,
    parent_id: int | None,
    actor: FolderActor,
    *,
    system: bool,
    managed_only: bool = True,
) -> tuple[LibraryFolder, bool]:
    """The folder called ``name`` under ``parent_id`` the actor puts files into (§5.2).

    Two folders of one name under one parent are normal now (mine and someone's private
    one), so a lookup by name takes the first, by id, the actor may write — never
    ``scalar_one_or_none``. A ``system`` folder (MakerWorld, Manyfold, Telegram, a
    product's folder) is everyone's: a shared one answers first, so every importer —
    whoever it acts for — lands in the same folder; then one of the actor's OWN (a product
    folder imported before m268 became its importer's, and must be found again — acceptance
    A01); never someone else's private folder that happens to carry the name, even for an
    actor who may write anywhere. None → a new folder: shared and ownerless for a
    system folder or under an external parent, else the actor's own. ``managed_only``:
    an external folder never answers (the importers'); a ZIP's path segments keep finding
    the share's own subfolders. ``products`` is loaded. Flushes, never commits.

    Returns the folder and whether it was made just now.
    """
    from sqlalchemy.orm import selectinload  # noqa: PLC0415

    parent_clause = LibraryFolder.parent_id.is_(None) if parent_id is None else LibraryFolder.parent_id == parent_id
    query = (
        select(LibraryFolder)
        .where(LibraryFolder.name == name, parent_clause)
        .options(selectinload(LibraryFolder.products))
        .order_by(LibraryFolder.id)
    )
    if managed_only:
        query = query.where(LibraryFolder.is_external.is_(False))
    candidates = (await db.execute(query)).scalars().all()
    if system:
        # Shared first, then the actor's own; another user's private folder never.
        candidates = [f for f in candidates if f.shared] + [
            f for f in candidates if not f.shared and owns(f, actor.user)
        ]
    for folder in candidates:
        if can_write_folder(folder, actor):
            return folder, False
    if system:
        owner_id, shared = None, True
    else:
        parent = await db.get(LibraryFolder, parent_id) if parent_id is not None else None
        owner_id, shared = new_folder_owner(parent, actor)
    folder = LibraryFolder(name=name, parent_id=parent_id, created_by_id=owner_id, shared=shared)
    db.add(folder)
    await db.flush()
    await db.refresh(folder, ["products"])
    return folder, True


async def result_folder_id(db: AsyncSession, source_folder_id: int | None, actor: FolderActor) -> int | None:
    """Where a result made from a file goes (slice, assembly; §5.3): beside its source when
    the actor may still write there, else the root. Asked when the result is PUBLISHED — the
    folder may have gone private, or gone, while the job ran."""
    if source_folder_id is None:
        return None
    folder = (await db.execute(select(LibraryFolder).where(LibraryFolder.id == source_folder_id))).scalar_one_or_none()
    if folder is None or not can_write_folder(folder, actor):
        return None
    return folder.id


def new_folder_owner(parent: LibraryFolder | None, actor: FolderActor) -> tuple[int | None, bool]:
    """``(created_by_id, shared)`` of a folder the actor makes under ``parent`` (§5.2): under
    an external parent it is the scanner's shape — shared, no owner; else the actor's own."""
    if parent is not None and parent.is_external:
        return None, True
    if actor.user is None:
        return None, True
    return actor.user.id, False


def folder_flags(
    folder: LibraryFolder,
    index: FolderIndex,
    actor: FolderActor,
    *,
    modify_all: bool,
    may_update_own: bool,
    delete_all: bool,
    may_delete_own: bool,
) -> dict:
    """What the File Manager may offer on ``folder`` for this caller — the routes enforce
    the same rules."""
    rename = can_rename_folder(folder, actor.user, modify_all=modify_all) and (modify_all or may_update_own)
    share = can_share_folder(folder, actor.user, modify_all=modify_all) and (modify_all or may_update_own)
    delete = (delete_all or may_delete_own) and folder_delete_blocker(
        index, folder, actor.user, delete_all=delete_all
    ) is None
    return {
        "created_by_id": folder.created_by_id,
        "shared": bool(folder.shared),
        "can_write": can_write_folder(folder, actor),
        "can_rename": rename,
        "can_share": share,
        "can_delete": delete,
    }
