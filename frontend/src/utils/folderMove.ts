import { ApiError } from '../api/client';
import type { LibraryFolderTree, Permission } from '../api/client';
import { canWriteInto, flattenFolderTree } from './folderTree';

/**
 * Where a library folder may be moved — the same facts `PUT /library/folders/{id}`
 * decides by, so the dialog and a drag can say "no" before the server does.
 * The server stays the authority: a tree read a moment ago may have changed.
 */

/** Why a target is not offered. `current` is "already there", not a refusal. */
export type FolderMoveBlock = 'self' | 'descendant' | 'external' | 'notWritable' | 'current';

/**
 * Moving asks what renaming asks: `library:update_all` moves any folder, and the owner
 * with `library:update_own` their own (N56) — the folder's `can_rename` says which.
 */
export function canMoveFolders(hasPermission: (permission: Permission) => boolean): boolean {
  return hasPermission('library:update_all') || hasPermission('library:update_own');
}

/** May the current user move THIS folder? The server's `can_rename`, else the old rule. */
export function canMoveFolder(
  folder: LibraryFolderTree,
  hasPermission: (permission: Permission) => boolean,
): boolean {
  return folder.can_rename ?? hasPermission('library:update_all');
}

/** An external folder is a directory of its share: neither a mount point nor a subfolder moves. */
export function isMovableFolder(folder: LibraryFolderTree): boolean {
  return !folder.is_external;
}

/** Ids of every folder that is external or lies under an external one. */
export function externalFolderIds(tree: LibraryFolderTree[] | undefined | null): Set<number> {
  const ids = new Set<number>();
  const walk = (folder: LibraryFolderTree, under: boolean) => {
    const external = under || folder.is_external;
    if (external) ids.add(folder.id);
    for (const child of folder.children ?? []) walk(child, external);
  };
  for (const root of tree ?? []) walk(root, false);
  return ids;
}

/** `null` when `source` may be moved under `targetId` (`null` = the library root). */
export function folderMoveBlock(
  source: LibraryFolderTree,
  targetId: number | null,
  tree: LibraryFolderTree[] | undefined | null,
): FolderMoveBlock | null {
  if (targetId === (source.parent_id ?? null)) return 'current';
  if (targetId === null) return null;
  if (targetId === source.id) return 'self';
  if (flattenFolderTree(source).some((row) => row.folder.id === targetId)) return 'descendant';
  if (externalFolderIds(tree).has(targetId)) return 'external';
  // N56: a folder goes only into one the user may add to.
  const target = (tree ?? []).flatMap((root) => flattenFolderTree(root)).find((row) => row.folder.id === targetId);
  if (target && !canWriteInto(target.folder)) return 'notWritable';
  return null;
}

/** The server's refusal codes for a folder move or rename, each with its sentence. */
const REFUSAL_KEYS: Record<string, string> = {
  folder_parent_not_found: 'fileManager.moveFolder.refusal.parentNotFound',
  folder_parent_external: 'fileManager.moveFolder.refusal.parentExternal',
  folder_external_fixed: 'fileManager.moveFolder.refusal.externalFixed',
  folder_own_subtree: 'fileManager.moveFolder.refusal.ownSubtree',
  folder_external_rename: 'fileManager.moveFolder.refusal.externalRename',
};

/**
 * The sentence for a refused folder move — by the refusal's CODE, never its text;
 * a refusal without a known code keeps the server's own (already translated) message.
 */
export function folderMoveRefusalText(t: (key: string) => string, error: unknown): string {
  const key = error instanceof ApiError && error.code ? REFUSAL_KEYS[error.code] : undefined;
  if (key) return t(key);
  return error instanceof Error ? error.message : String(error);
}
