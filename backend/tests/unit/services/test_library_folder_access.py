"""The folders' one rule (N56, ``services/library_folder_access``), over an index built by hand.

D1: a ``library:read_own`` actor sees their own folders, shared ones, those holding a file the
file rule shows them (their own, or an ownerless one of an external folder) and the folders above
those — for navigation only; they write into their own, shared ones and the root. D3: the owner
shares their folder, ``library:update_all`` any; an external folder stays shared. §5.6: the owner
deletes a folder when everything under it is theirs.
"""

from types import SimpleNamespace

from backend.app.models.library import LibraryFolder
from backend.app.services.library_folder_access import (
    FolderActor,
    FolderIndex,
    can_rename_folder,
    can_share_folder,
    can_write_folder,
    folder_delete_blocker,
    new_folder_owner,
    visible_folder_ids,
)

ALICE = SimpleNamespace(id=1)
BOB = SimpleNamespace(id=2)


def _folder(fid, parent=None, *, owner=None, shared=False, external=False, name=None):
    return LibraryFolder(
        id=fid, parent_id=parent, created_by_id=owner, shared=shared, is_external=external, name=name or f"f{fid}"
    )


def _index(folders, files=(), linked=()):
    """``files``: (folder, owner, live)."""
    index = FolderIndex()
    for folder in folders:
        index.folders[folder.id] = folder
        index.children.setdefault(folder.parent_id, []).append(folder.id)
    for folder_id, owner, live in files:
        index.file_owners.setdefault(folder_id, set()).add(owner)
        if live:
            index.live_file_owners.setdefault(folder_id, set()).add(owner)
    index.linked = set(linked)
    return index


def _tree():
    return _index(
        [
            _folder(1, owner=BOB.id),  # bob's private root …
            _folder(2, 1, owner=BOB.id),  # … with alice's file inside a private subfolder
            _folder(3, owner=BOB.id),  # bob's, nothing of alice's
            _folder(4, owner=BOB.id, shared=True),  # shared
            _folder(5, owner=ALICE.id),  # alice's own
            _folder(6, external=True),  # a mount, not marked shared, with an ownerless file
            _folder(7, owner=BOB.id),  # bob's, alice's file only in the TRASH
        ],
        files=[(2, ALICE.id, True), (3, BOB.id, True), (6, None, True), (7, ALICE.id, False)],
    )


def test_read_own_sees_own_shared_their_files_folders_and_the_way_there():
    visible = visible_folder_ids(_tree(), FolderActor(ALICE, False))

    assert visible == {1, 2, 4, 5, 6}


def test_reading_everything_sees_every_folder():
    assert visible_folder_ids(_tree(), FolderActor(ALICE, True)) is None


def test_a_chat_without_a_user_sees_only_shared_folders():
    assert visible_folder_ids(_tree(), FolderActor(None, False)) == {4, 6}


def test_writes_go_to_own_shared_and_root_only():
    index = _tree()
    alice = FolderActor(ALICE, False)

    writable = {fid for fid, folder in index.folders.items() if can_write_folder(folder, alice)}

    assert writable == {4, 5}  # the parent of her file is navigation only
    assert can_write_folder(None, alice)
    assert all(can_write_folder(folder, FolderActor(ALICE, True)) for folder in index.folders.values())


def test_the_owner_renames_and_shares_update_all_any_and_an_external_folder_stays_shared():
    mine, theirs, mount = _folder(1, owner=ALICE.id), _folder(2, owner=BOB.id), _folder(3, external=True)

    assert can_rename_folder(mine, ALICE, modify_all=False)
    assert not can_rename_folder(theirs, ALICE, modify_all=False)
    assert can_rename_folder(theirs, ALICE, modify_all=True)
    assert can_share_folder(mine, ALICE, modify_all=False)
    assert not can_share_folder(theirs, ALICE, modify_all=False)
    assert can_share_folder(theirs, ALICE, modify_all=True)
    assert not can_share_folder(mount, ALICE, modify_all=True)


def test_a_new_folder_is_the_actors_own_unless_under_a_share_or_without_a_user():
    assert new_folder_owner(None, FolderActor(ALICE, False)) == (ALICE.id, False)
    assert new_folder_owner(_folder(1, owner=BOB.id, shared=True), FolderActor(ALICE, False)) == (ALICE.id, False)
    assert new_folder_owner(_folder(1, external=True), FolderActor(ALICE, False)) == (None, True)
    assert new_folder_owner(None, FolderActor(None, True)) == (None, True)


def test_the_owner_deletes_a_folder_when_everything_under_it_is_theirs():
    index = _index(
        [
            _folder(1, owner=ALICE.id),
            _folder(2, 1, owner=ALICE.id),
            _folder(3, owner=ALICE.id),
            _folder(4, 3, owner=BOB.id),  # someone else's subfolder
            _folder(5, owner=ALICE.id),  # someone else's TRASHED file
            _folder(6, owner=ALICE.id),  # bound to a product
            _folder(7),  # ownerless, empty
            _folder(8),  # ownerless, holding a file
        ],
        files=[(2, ALICE.id, True), (1, ALICE.id, False), (5, BOB.id, False), (8, ALICE.id, True)],
        linked=[6],
    )
    folders = index.folders

    assert folder_delete_blocker(index, folders[1], ALICE, delete_all=False) is None
    assert "other users" in folder_delete_blocker(index, folders[3], ALICE, delete_all=False)
    assert "files of other users" in folder_delete_blocker(index, folders[5], ALICE, delete_all=False)
    assert "linked to a product" in folder_delete_blocker(index, folders[6], ALICE, delete_all=False)
    assert "owner" in folder_delete_blocker(index, folders[1], BOB, delete_all=False)
    assert folder_delete_blocker(index, folders[7], BOB, delete_all=False) is None
    assert "empty" in folder_delete_blocker(index, folders[8], ALICE, delete_all=False)
    assert folder_delete_blocker(index, folders[4], ALICE, delete_all=True) is None


def test_a_parent_loop_cannot_hang_the_rule():
    index = _index([_folder(1, 2, owner=ALICE.id), _folder(2, 1, owner=ALICE.id)])

    assert visible_folder_ids(index, FolderActor(ALICE, False)) == {1, 2}
    assert folder_delete_blocker(index, index.folders[1], ALICE, delete_all=False) is None
