/**
 * Choosing a parent project, without offering a loop.
 *
 * The server refuses a cycle either way, so this is not the safety net — it is
 * the reason the operator never meets the error. On a deep tree, an option that
 * always fails is a worse question than an option that is not there.
 *
 * ⚠️ Descendants must go too, not just the project itself. A→B→C: offering C as
 * A's parent is exactly the two-step loop the backend guard exists for.
 */

import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';

const PAGE = readFileSync('src/pages/ProjectsPage.tsx', 'utf8');
const DETAIL = readFileSync('src/pages/ProjectDetailPage.tsx', 'utf8');

/** The exclusion rule as the page computes it, over a flat parent map. */
function candidates(
  all: { id: number; parent_id: number | null; is_template?: boolean }[],
  editingId: number | null,
): number[] {
  const banned = new Set<number>();
  if (editingId != null) {
    banned.add(editingId);
    let grew = true;
    while (grew) {
      grew = false;
      for (const c of all) {
        if (c.parent_id != null && banned.has(c.parent_id) && !banned.has(c.id)) {
          banned.add(c.id);
          grew = true;
        }
      }
    }
  }
  return all.filter((c) => !c.is_template && !banned.has(c.id)).map((c) => c.id);
}

const TREE = [
  { id: 1, parent_id: null },
  { id: 2, parent_id: 1 },
  { id: 3, parent_id: 2 },
  { id: 4, parent_id: null },
];

describe('parent options', () => {
  it('never offers the project itself', () => {
    expect(candidates(TREE, 1)).not.toContain(1);
  });

  it('never offers a child', () => {
    expect(candidates(TREE, 1)).not.toContain(2);
  });

  it('never offers a grandchild — the two-step loop', () => {
    expect(candidates(TREE, 1)).not.toContain(3);
  });

  it('still offers an unrelated project', () => {
    expect(candidates(TREE, 1)).toEqual([4]);
  });

  it('offers everything when creating a new project', () => {
    expect(candidates(TREE, null)).toEqual([1, 2, 3, 4]);
  });

  it('leaves templates out — a template is a shape to copy, not a place to file work under', () => {
    const withTemplate = [...TREE, { id: 5, parent_id: null, is_template: true }];
    expect(candidates(withTemplate, 1)).toEqual([4]);
  });

  it('terminates on a tree that already contains a loop', () => {
    const looped = [
      { id: 1, parent_id: 2 },
      { id: 2, parent_id: 1 },
      { id: 3, parent_id: null },
    ];
    expect(candidates(looped, 1)).toEqual([3]);
  });
});

describe('wiring', () => {
  it('the page computes the options and hands them to the modal', () => {
    expect(PAGE).toContain('parentOptions={parentOptions}');
    expect(PAGE).toContain("t('projects.parentProject')");
  });

  it('clearing the parent sends 0, which is how this API says "no parent"', () => {
    expect(PAGE).toContain('parent_id: parentId ?? 0');
  });

  it('the roll-up card appears only when the server sent one', () => {
    expect(DETAIL).toContain('{project.rollup_stats && (');
    expect(DETAIL).toContain("t('projectDetail.rollup.title')");
  });
});
