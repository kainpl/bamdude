/**
 * Flattening the library folder tree for a picker.
 *
 * ⚠️ `writableFolders` drops read-only external mounts because the backend
 * answers an upload into one with 403. Offering a choice that cannot work is
 * worse than not offering it — and every picker had this filter written out
 * inline, so extracting the flatten alone would have replaced three copies of a
 * function with three copies of its filter.
 */

import { describe, it, expect } from 'vitest';
import { flattenFolderTree, writableFolders } from '../../utils/folderTree';
import type { LibraryFolderTree } from '../../api/client';

const f = (id: number, name: string, extra: Partial<LibraryFolderTree> = {}): LibraryFolderTree =>
  ({ id, name, children: [], is_external: false, external_readonly: false, ...extra }) as LibraryFolderTree;

describe('flattenFolderTree', () => {
  it('keeps nesting as a depth', () => {
    const tree = f(1, 'root', { children: [f(2, 'child', { children: [f(3, 'grandchild')] })] } as never);

    expect(flattenFolderTree(tree).map((x) => [x.folder.id, x.depth])).toEqual([
      [1, 0],
      [2, 1],
      [3, 2],
    ]);
  });

  it('survives a folder with no children array at all', () => {
    expect(flattenFolderTree({ id: 1, name: 'x' } as LibraryFolderTree)).toHaveLength(1);
  });
});

describe('writableFolders', () => {
  it('drops read-only external mounts', () => {
    const trees = [f(1, 'normal'), f(2, 'ro', { is_external: true, external_readonly: true })];

    expect(writableFolders(trees).map((x) => x.folder.id)).toEqual([1]);
  });

  it('keeps a writable external mount', () => {
    /* External is not the problem — read-only is. */
    const trees = [f(2, 'nas', { is_external: true, external_readonly: false })];

    expect(writableFolders(trees).map((x) => x.folder.id)).toEqual([2]);
  });

  it('answers an empty list for nothing at all', () => {
    expect(writableFolders(undefined)).toEqual([]);
    expect(writableFolders(null)).toEqual([]);
  });
});
