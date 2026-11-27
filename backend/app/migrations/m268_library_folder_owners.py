"""Library folders get an owner and can be shared (N56, upstream Bambuddy 6735e4556, #3201).

Folders had no owner, so a ``library:read_own`` user saw every folder anyone had made
and was offered each as an upload target. ``created_by_id`` names who made a folder;
``shared`` makes it everyone's (``services/library_folder_access`` is the one rule).

The existing folders are settled here, once, in this migration's own transaction —
only when ``shared`` was just added, so a DEBUG replay (``replay`` adds the columns
only) never undoes what an admin chose afterwards. Owner decision 2026-11-27, as
upstream: a folder whose files, all the way down and trashed ones included, belong to
one user becomes that user's; an empty folder inside it goes with it. Every other
folder is shared, so it stays visible as before: empty, several uploaders or files
without an owner, external, bound to a product, a Virtual Printer's target, and the
top-level ``MakerWorld``, ``Manyfold`` and ``Telegram`` folders every importer shares.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, table_exists

version = 268
name = "library_folder_owners"

# The root folders every importer lands in (model_providers/*/provider.py
# ``default_folder_name``; telegram_handlers/library_upload_scene ``DEFAULT_FOLDER_NAME``).
SHARED_ROOT_NAMES = frozenset({"MakerWorld", "Manyfold", "Telegram"})


async def _columns(conn) -> bool:
    """Add both columns; True when ``shared`` was added just now."""
    await add_column(conn, "library_folders", "created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    return await add_column(conn, "library_folders", "shared BOOLEAN NOT NULL DEFAULT FALSE")


async def upgrade(conn):
    if await _columns(conn):
        await _settle_existing_folders(conn)


async def replay(conn):
    """DEBUG start: the columns only — never the backfill over an admin's later choices."""
    await _columns(conn)


async def _settle_existing_folders(conn) -> None:
    folders = (await conn.execute(text("SELECT id, parent_id, is_external, name FROM library_folders"))).all()
    if not folders:
        return
    info = {fid: (parent_id, bool(is_ext), name) for fid, parent_id, is_ext, name in folders}
    children: dict[int | None, list[int]] = {}
    for fid, (parent_id, _ext, _name) in info.items():
        children.setdefault(parent_id, []).append(fid)

    own_owners: dict[int, set] = {}
    for folder_id, owner_id in (
        await conn.execute(
            text("SELECT DISTINCT folder_id, created_by_id FROM library_files WHERE folder_id IS NOT NULL")
        )
    ).all():
        own_owners.setdefault(folder_id, set()).add(owner_id)

    pinned: set[int] = set()  # always shared: bound to a product, a VP's target
    if await table_exists(conn, "product_folders"):
        pinned |= set(
            (await conn.execute(text("SELECT DISTINCT library_folder_id FROM product_folders"))).scalars().all()
        )
    if await table_exists(conn, "virtual_printers"):
        pinned |= set(
            (
                await conn.execute(
                    text("SELECT DISTINCT target_folder_id FROM virtual_printers WHERE target_folder_id IS NOT NULL")
                )
            )
            .scalars()
            .all()
        )

    subtree_owners: dict[int, set] = {}

    def owners_of(root: int) -> set:
        # ``seen`` guards a parent_id loop: start-up must never hang.
        order: list[int] = []
        seen: set[int] = set()
        stack = [root]
        while stack:
            fid = stack.pop()
            if fid in seen:
                continue
            seen.add(fid)
            order.append(fid)
            stack.extend(children.get(fid, []))
        for fid in reversed(order):
            if fid not in subtree_owners:
                acc = set(own_owners.get(fid, set()))
                for child in children.get(fid, []):
                    acc |= subtree_owners.get(child, set())
                subtree_owners[fid] = acc
        return subtree_owners[root]

    result: dict[int, int | None] = {}
    # Top down, so an empty folder can follow its parent's owner. A folder whose parent
    # is missing (should not happen with the FK) is a root too.
    stack = list(children.get(None, []))
    stack.extend(fid for fid, (parent_id, *_rest) in info.items() if parent_id is not None and parent_id not in info)
    while stack:
        fid = stack.pop()
        if fid in result:
            continue
        parent_id, is_ext, name = info[fid]
        owner = None
        import_root = parent_id is None and name in SHARED_ROOT_NAMES
        if not is_ext and fid not in pinned and not import_root:
            owners = owners_of(fid)
            if len(owners) == 1 and None not in owners:
                owner = next(iter(owners))
            elif not owners and parent_id is not None:
                owner = result.get(parent_id)
        result[fid] = owner
        stack.extend(children.get(fid, []))
    for fid in info:
        result.setdefault(fid, None)

    owned = [{"id": fid, "o": owner} for fid, owner in result.items() if owner is not None]
    if owned:
        await conn.execute(text("UPDATE library_folders SET created_by_id = :o, shared = FALSE WHERE id = :id"), owned)
    shared = [{"id": fid} for fid, owner in result.items() if owner is None]
    if shared:
        await conn.execute(text("UPDATE library_folders SET shared = TRUE WHERE id = :id"), shared)
