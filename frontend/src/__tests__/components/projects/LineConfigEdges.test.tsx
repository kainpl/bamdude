/**
 * The edges of a line's configuration the WS-08 review found, on the page:
 * a completed order is not reconfigured, Save waits for the warning, and the
 * kits offered from the shelf are the chosen configuration's.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order, Product, ProjectLine } from '../../../api/client';
import { LineConfigDialog } from '../../../components/projects/LineConfigDialog';
import { OrderLinesTable } from '../../../components/projects/OrderLinesTable';

const product = {
  id: 7,
  name: 'Pipe',
  parts: [
    { id: 5, kind: 'printed', name: 'Колба', qty_per_unit: 1, sort_order: 0, variant_option_ids: [] },
    { id: 6, kind: 'printed', name: 'straight tail', qty_per_unit: 1, sort_order: 1, variant_option_ids: [11] },
    { id: 8, kind: 'printed', name: 'angled tail', qty_per_unit: 1, sort_order: 2, variant_option_ids: [12] },
  ],
  variant_groups: [
    {
      id: 3,
      name: 'Хвіст',
      position: 0,
      default_option_id: 11,
      options: [
        { id: 11, name: 'прямий', position: 0, lines_count: 1, parts_count: 1 },
        { id: 12, name: 'кутовий', position: 1, lines_count: 0, parts_count: 1 },
      ],
    },
  ],
} as unknown as Product;

const line = {
  id: 21,
  product_id: 7,
  product_name: 'Pipe',
  quantity: 4,
  material: null,
  color: null,
  note: null,
  sort_order: 0,
  units_printed: 0,
  from_stock_units: 1,
  from_kit_units: 1,
  from_finished: 0,
  covered_units: 1,
  progress: 0.25,
  archive_ids: [],
  parts: [],
  mode: 'product',
  config_key: '3=12',
  configuration: {
    choices: [{ group_id: 3, group_name: 'Хвіст', option_id: 12, option_name: 'кутовий', is_default: false }],
    changed_parts: [],
  },
} as unknown as ProjectLine;

describe('a completed order', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('does not offer to reconfigure its lines, and says why', async () => {
    render(<OrderLinesTable order={{ id: 9, status: 'completed', lines: [line] } as unknown as Order} canEdit />);
    // WS-13 E4 B06: the row's menu item, where the icon button was.
    fireEvent.click(screen.getByRole('button', { name: 'Line actions: Pipe' }));
    const button = within(await screen.findByRole('menu')).getByRole('menuitem', { name: 'Part configuration…' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', expect.stringMatching(/reopen/i));
  });
});

describe('LineConfigDialog · save waits for the warning', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(product);
  });

  it('keeps Save disabled until the impact is on screen', async () => {
    let answer: (v: unknown) => void = () => {};
    vi.spyOn(api, 'previewLineConfiguration').mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }) as never,
    );
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={line} onClose={() => {}} />);
    // WS-13 E5 F03: the group is radios now.
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: /прямий/ }));
    expect(screen.getByRole('button', { name: 'Save configuration' })).toBeDisabled();
    answer({ reserved_before: 1, reserved_after: 1, dropping: [] });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save configuration' })).toBeEnabled());
  });
});

describe('kits offered from the shelf', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProducts').mockResolvedValue([{ id: 7, code: 'PR-0007', name: 'Pipe', is_active: true }] as never);
    vi.spyOn(api, 'getProduct').mockResolvedValue(product);
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ kits_by_option: [], kits_available: 5, balances: [], movements: [] });
  });

  it('are the line’s own configuration’s when editing it', async () => {
    const kits = vi.spyOn(api, 'getConfigurationKits').mockResolvedValue({ kits_available: 2 });
    render(<OrderLinesTable order={{ id: 9, status: 'active', lines: [line] } as unknown as Order} canEdit />);
    // WS-13 E4 D01: «Edit line…» from the row's menu opens the dialog the cells became.
    fireEvent.click(screen.getByRole('button', { name: 'Line actions: Pipe' }));
    fireEvent.click(within(await screen.findByRole('menu')).getByRole('menuitem', { name: 'Edit line…' }));
    await waitFor(() => expect(kits).toHaveBeenCalledWith(7, { options: [12], counts: {} }));
    // Two free kits of the angled configuration plus the one this line holds.
    await waitFor(() => expect(screen.getByLabelText('From stock — part kits')).toHaveAttribute('max', '3'));
  });
});
