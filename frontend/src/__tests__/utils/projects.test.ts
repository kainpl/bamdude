/**
 * Which orders and products may be offered for binding.
 *
 * ⚠️ **The trap this is mostly here to pin:** a row already bound must stay on
 * the list even when it is closed or out of the catalog. Hiding it renders the
 * field as "nothing chosen", and the next save writes that emptiness back — the
 * filter would quietly destroy the binding it was meant to tidy around.
 *
 * ⚠️ **`completed` is NOT offered.** An order has three statuses — `active`,
 * `completed`, `cancelled` — and only the first is open work. A reprint for a
 * closed order is filed by reopening it, not by quietly adding to a finished
 * ledger.
 */

import { describe, it, expect } from 'vitest';
import { selectableProjects, selectableProducts } from '../../utils/projects';

const ACTIVE = { id: 1, status: 'active' };
const COMPLETED = { id: 2, status: 'completed' };
const CANCELLED = { id: 3, status: 'cancelled' };
const ALL = [ACTIVE, COMPLETED, CANCELLED];

describe('selectableProjects', () => {
  it('offers active orders only', () => {
    expect(selectableProjects(ALL)).toEqual([ACTIVE]);
  });

  it('keeps a closed order that is already bound', () => {
    expect(selectableProjects(ALL, [2])).toEqual([ACTIVE, COMPLETED]);
    // The multi-select dialogs hold the current binding as a Set.
    expect(selectableProjects(ALL, new Set([3]))).toEqual([ACTIVE, CANCELLED]);
  });

  it('does not resurrect an unbound closed order', () => {
    expect(selectableProjects(ALL, [1])).toEqual([ACTIVE]);
  });

  it('tolerates empty input and rows without a status', () => {
    expect(selectableProjects(undefined)).toEqual([]);
    expect(selectableProjects(null)).toEqual([]);
    expect(selectableProjects([{ id: 9 }])).toEqual([]);
  });
});

describe('selectableProducts', () => {
  const IN = { id: 1, is_active: true };
  const OUT = { id: 2, is_active: false };

  it('offers catalog products only, plus the bound one', () => {
    expect(selectableProducts([IN, OUT])).toEqual([IN]);
    expect(selectableProducts([IN, OUT], [2])).toEqual([IN, OUT]);
  });

  it('treats a missing flag as in the catalog', () => {
    expect(selectableProducts([{ id: 3 }])).toEqual([{ id: 3 }]);
  });

  it('tolerates empty input', () => {
    expect(selectableProducts(undefined)).toEqual([]);
    expect(selectableProducts(null)).toEqual([]);
  });

  it('hides adhoc products unless the row already links them', () => {
    const products = [
      { id: 1, is_active: true, origin: 'catalog' as const },
      { id: 2, is_active: true, origin: 'adhoc_job' as const },
      { id: 3, is_active: true, origin: 'adhoc_plate' as const },
    ];
    expect(selectableProducts(products).map((p) => p.id)).toEqual([1]);
    expect(selectableProducts(products, [3]).map((p) => p.id)).toEqual([1, 3]);
    // A row from an older server carries no origin and stays selectable.
    expect(selectableProducts([{ id: 4, is_active: true }]).map((p) => p.id)).toEqual([4]);
  });
});
