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
    // ⚠️ `copies` is the SAME number the request carries as its `quantity` —
    // since the «Total» quantity mode (spec 2026-09-11) deals a different one
    // to each printer, the rows counter must add what was SENT, never the
    // field's own figure. Both now read one helper, `dealtCopies`, which is
    // also what decides who is skipped and whose spools are weighed — so the
    // helper's own body is pinned here too.
    expect(source).toMatch(/const dealtCopies = \([\s\S]{0,120}?mode === 'edit-queue-item' \? 1 : copiesFor\(plateIndex, printerId\)/);
    expect(source).toMatch(/const copies = dealtCopies\(plateId, printerId\);/);
    expect(source).toMatch(/quantity: dealtCopies\(plateId, printerId\),/);
    expect(source).toMatch(/results\.queued \+= copies;/);
  });

  it('keeps attempts and rows as separate counters', () => {
    // ⚠️ `success`/`failed` stay a pair of ATTEMPT counts — the partial-failure
    // toast pairs them, and "3 of 4 printers failed" is about printers. Folding
    // the two meanings into one number is how this went wrong.
    expect(source).toContain('success: number; failed: number; queued: number');
    expect(source).toContain("t('printModal.partialSuccess', { success: results.success, failed: results.failed })");
    // ⚠️ Since m173 the add path leads with whether a job exists at all
    // (spec §10). That sentence pairs the same two ATTEMPT counts — an add is
    // all-or-nothing per request, so rows have no business in it.
    expect(source).toContain(
      'queueAddFailureText(t, firstFailure, { added: results.success, total: results.success + results.failed })',
    );
  });
});
