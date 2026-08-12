import type { LibraryFolderTree } from '../api/client';

/** One folder plus how deep it sits, so a flat list can indent its label. */
export type FlatFolder = { folder: LibraryFolderTree; depth: number };

/**
 * Depth-first flatten of the library folder tree.
 *
 * The API returns folders nested; a picker needs them flat with the nesting
 * preserved as an indent. This existed as FOUR private copies — the
 * file-manager modal, the virtual-printer card, the MakerWorld page, and the
 * "Move files" dialog, that last one returning a different shape.
 *
 * Nothing outside `FolderTreePicker` calls either of these now: the picker is
 * the one place a folder tree becomes a list. Both stay exported because the
 * picker needs `writableFolders` for the normal case and this primitive for
 * `includeReadOnly` — a picker that only READS folders has no reason to hide
 * the read-only ones.
 */
export function flattenFolderTree(
  tree: LibraryFolderTree,
  depth = 0,
  out: FlatFolder[] = [],
): FlatFolder[] {
  out.push({ folder: tree, depth });
  for (const child of tree.children ?? []) {
    flattenFolderTree(child, depth + 1, out);
  }
  return out;
}

/**
 * Folders that can be written into, flattened.
 *
 * ⚠️ Read-only external mounts are dropped because the backend answers a write
 * into one with **403** — an upload, and equally a move, which says "Cannot
 * move files to a read-only external folder". Offering a choice that cannot
 * work is worse than not offering it. Every picker had this same filter inline;
 * extracting `flattenFolderTree` alone would have replaced copies of a function
 * with copies of its filter.
 */
export function writableFolders(trees: LibraryFolderTree[] | undefined | null): FlatFolder[] {
  return (trees ?? [])
    .filter((f) => !(f.is_external && f.external_readonly))
    .flatMap((f) => flattenFolderTree(f));
}
