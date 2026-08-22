/**
 * The "N items queued" toast counts queue entries, not requests.
 *
 * ⚠️ Reported from a farm: four printers picked, two copies each, and the toast
 * said four. It counted the POSTs it made — one per printer — while the server
 * writes one row per copy inside each of them (`for i in range(data.quantity)`
 * in `queue_add`). Eight entries landed; the operator was told four.
 *
 * ⚠️ **Structural, and weaker than it should be.** The arithmetic lives inside
 * `handleSubmit`, a several-hundred-line handler with no seam to call, and
 * driving the modal far enough to submit needs more fixture than the fix is
 * worth. What is pinned here is the distinction the bug erased: two counters,
 * one for attempts and one for rows, and the toast reading the second.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';

const source = readFileSync('src/components/PrintModal/index.tsx', 'utf-8');

describe('the queued-count toast', () => {
  it('reports rows, not requests', () => {
    expect(source).toContain("t('queue.itemsQueued', { count: results.queued })");
    expect(source).not.toContain("t('queue.itemsQueued', { count: results.success })");
  });

  it('counts a request as as many rows as it carries copies', () => {
    expect(source).toMatch(/results\.queued \+= mode === 'edit-queue-item' \? 1 : quantityForPlate\(plateId\)/);
  });

  it('keeps attempts and rows as separate counters', () => {
    // ⚠️ `success`/`failed` stay a pair of ATTEMPT counts — the partial-failure
    // toast pairs them, and "3 of 4 printers failed" is about printers. Folding
    // the two meanings into one number is how this went wrong.
    expect(source).toContain('success: number; failed: number; queued: number');
    expect(source).toContain("t('printModal.partialSuccess', { success: results.success, failed: results.failed })");
  });
});
