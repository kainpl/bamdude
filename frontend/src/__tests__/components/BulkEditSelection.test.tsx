/**
 * BulkEditSpoolsModal after the page gained its own selection (#1795).
 *
 * The modal used to BE the selection mechanism: it received the whole filtered
 * inventory, pre-ticked every row, and let you narrow it with checkboxes
 * inside the dialog. With row checkboxes and a toolbar on the page, a second
 * set of checkboxes here would be a competing source of truth for the same
 * question — so the pane is now read-only.
 *
 * It is deliberately still a list, not just a count: it is the only thing
 * between the user and a mass edit of the wrong spools.
 */

import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { BulkEditSpoolsModal } from '../../components/BulkEditSpoolsModal';
import type { InventorySpool } from '../../api/client';

const spool = (id: number, brand: string): InventorySpool =>
  ({
    id,
    brand,
    material: 'PLA',
    color_name: 'Red',
    rgba: 'FF0000FF',
    label_weight: 1000,
    weight_used: 0,
    k_profiles: [],
  }) as unknown as InventorySpool;

function renderModal(spools: InventorySpool[]) {
  return render(
    <BulkEditSpoolsModal
      isOpen
      spools={spools}
      allSpools={spools}
      catalogEntries={[]}
      spoolDisplayTemplate=""
      onClose={vi.fn()}
      onSaved={vi.fn()}
    />,
  );
}

describe('BulkEditSpoolsModal selection pane', () => {
  it('lists exactly the spools it was handed', () => {
    renderModal([spool(1, 'Alpha'), spool(2, 'Beta')]);

    expect(screen.getByText(/Alpha/)).toBeInTheDocument();
    expect(screen.getByText(/Beta/)).toBeInTheDocument();
  });

  it('offers no checkboxes for narrowing the set', () => {
    // Every checkbox left in the dialog belongs to the FIELD editor (which
    // field to apply), never to spool selection — that now lives on the page.
    const { container } = renderModal([spool(1, 'Alpha'), spool(2, 'Beta')]);

    const checkboxes = container.querySelectorAll('input[type="checkbox"]');
    // Two spools; if the old per-spool checkboxes were still here there would
    // be at least two more than the field toggles, and the count would grow
    // with the number of spools. Assert it does not.
    const withTwo = checkboxes.length;

    const { container: c4 } = renderModal([
      spool(1, 'Alpha'),
      spool(2, 'Beta'),
      spool(3, 'Gamma'),
      spool(4, 'Delta'),
    ]);
    const withFour = c4.querySelectorAll('input[type="checkbox"]').length;

    expect(withFour).toBe(withTwo);
  });
});
