import type { LibraryFolderTree } from '../api/client';

/** One folder plus how deep it sits, so a flat `<select>` can indent its label. */
export type FlatFolder = { folder: LibraryFolderTree; depth: number };

/**
 * Depth-first flatten of the library folder tree.
 *
 * The API returns folders nested; every picker needs them flat with the nesting
 * preserved as an indent. This existed as three byte-identical private copies —
 * in the file-manager modal, the virtual-printer card and the MakerWorld page —
 * before a fourth was nearly written.
 *
 * ⚠️ Callers still apply their own filter. A picker that writes into the chosen
 * folder must drop read-only external ones (`is_external && external_readonly`),
 * because the backend answers those with 403 — but a picker that only reads has
 * no reason to hide them, so the rule belongs at the call site rather than here.
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

/** Folders that can be written into — everything except read-only external mounts. */
export function writableFolders(trees: LibraryFolderTree[] | undefined | null): FlatFolder[] {
  return (trees ?? [])
    .filter((f) => !(f.is_external && f.external_readonly))
    .flatMap((f) => flattenFolderTree(f));
}
