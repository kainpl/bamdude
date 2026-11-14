/**
 * A line's configuration (spec workshop-product-variants, rules 14, 26–27):
 * the caption under the product, the dialog that changes it — with the
 * server's dry-run impact shown before the save — and the parts-only line.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import i18n from '../../../i18n';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order, Product, ProjectLine } from '../../../api/client';
import { LineConfigDialog } from '../../../components/projects/LineConfigDialog';
import { lineConfigLabel } from '../../../components/projects/lineConfigLabel';

const t = i18n.t.bind(i18n);

const product = {
  id: 7,
  name: 'Pipe',
  parts: [
    { id: 5, kind: 'printed', name: 'Колба', qty_per_unit: 1, sort_order: 0, variant_option_ids: [] },
    { id: 6, kind: 'printed', name: 'straight tail', qty_per_unit: 1, sort_order: 1, variant_option_ids: [11] },
    { id: 8, kind: 'printed', name: 'angled tail', qty_per_unit: 1, sort_order: 2, variant_option_ids: [12] },
    // «Не рахувати» (spec workshop-order-issue-followups, rule 34): never offered to a line.
    { id: 9, kind: 'printed', name: 'test cube', qty_per_unit: 0, sort_order: 3, variant_option_ids: [], ignored: true },
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
  mode: 'product',
  config_key: '3=11',
  configuration: {
    choices: [{ group_id: 3, group_name: 'Хвіст', option_id: 11, option_name: 'прямий', is_default: true }],
    changed_parts: [],
  },
  from_stock_units: 4,
  parts: [],
} as unknown as ProjectLine;

describe('lineConfigLabel', () => {
  it('names the options that differ from the standard and counts the changed parts', () => {
    expect(
      lineConfigLabel(
        {
          choices: [{ group_id: 3, group_name: 'Хвіст', option_id: 12, option_name: 'кутовий', is_default: false }],
          changed_parts: [{ part_id: 5, name: 'Колба', qty: 2, standard_qty: 1 }],
        },
        'product',
        t,
      ),
    ).toBe('Хвіст: кутовий · 1 part changed');
  });

  it('says standard when every choice is the standard and nothing changed', () => {
    expect(lineConfigLabel(line.configuration, 'product', t)).toBe('standard');
  });

  it('says nothing for a product without variants', () => {
    expect(lineConfigLabel({ choices: [], changed_parts: [] }, 'product', t)).toBe('');
  });

  it('says parts only for a parts line', () => {
    expect(lineConfigLabel({ choices: [], changed_parts: [] }, 'parts', t)).toBe('parts only');
  });
});

describe('LineConfigDialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(product);
  });

  it('never offers a part marked not counted', async () => {
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={line} onClose={() => {}} />);
    expect(await screen.findByText('Колба')).toBeInTheDocument();
    expect(screen.queryByText('test cube')).not.toBeInTheDocument();
  });

  it('shows a choice per group and each part with its per-unit count', async () => {
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={line} onClose={() => {}} />);
    // WS-13 E5 F03: the group is radios, the standard one chosen.
    const group = await screen.findByRole('group', { name: 'Хвіст' });
    expect(within(group).getByRole('radio', { name: /прямий/ })).toBeChecked();
    expect(screen.getByLabelText('Колба — in the kit')).toBeChecked();
    expect(screen.getByLabelText('straight tail — in the kit')).toBeChecked();
    expect(screen.getByLabelText('angled tail — in the kit')).not.toBeChecked();
    // E5 F04: the field shows the count, with the standard beside it.
    expect(screen.getByLabelText('Колба — per unit')).toHaveValue(1);
    expect(screen.getAllByText('standard 1').length).toBeGreaterThan(0);
  });

  it('asks the server what a change would do before saving it', async () => {
    const preview = vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue({
      reserved_before: 4,
      reserved_after: 2,
      finished_before: 0,
      finished_after: 0,
      dropping: [{ part_id: 6, name: 'straight tail', per_before: 1, per_after: 0, printed: 3, queued: 1 }],
    });
    const save = vi.spyOn(api, 'setLineConfiguration').mockResolvedValue({ id: 9, lines: [] } as unknown as Order);
    const onClose = vi.fn();
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={line} onClose={onClose} />);
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));

    await waitFor(() => expect(preview).toHaveBeenCalledWith(9, 21, { choices: { 3: 12 }, part_counts: {} }));
    expect(await screen.findByText('straight tail: 3 printed, 1 queued will become surplus')).toBeInTheDocument();
    expect(screen.getByText('Kit reserve: 4 → 2')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(9, 21, { choices: { 3: 12 }, part_counts: {} }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('shows the ready units a change would move to the new position', async () => {
    vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue({
      reserved_before: 2,
      reserved_after: 1,
      finished_before: 2,
      finished_after: 0,
      dropping: [],
    });
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={line} onClose={() => {}} />);
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    expect(await screen.findByText('Kit reserve: 2 → 1')).toBeInTheDocument();
    expect(screen.getByText('Ready from stock: 2 → 0')).toBeInTheDocument();
  });

  it('resets to the standard options and counts', async () => {
    vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue({ reserved_before: 0, reserved_after: 0, finished_before: 0, finished_after: 0, dropping: [] });
    const save = vi.spyOn(api, 'setLineConfiguration').mockResolvedValue({ id: 9, lines: [] } as unknown as Order);
    const angled = {
      ...line,
      configuration: {
        choices: [{ group_id: 3, group_name: 'Хвіст', option_id: 12, option_name: 'кутовий', is_default: false }],
        changed_parts: [{ part_id: 5, name: 'Колба', qty: 3, standard_qty: 1 }],
      },
    } as ProjectLine;
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={angled} onClose={() => {}} />);
    expect(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' })).toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Reset to standard' }));
    // Save waits for the dry run's answer.
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(9, 21, { choices: { 3: 11 }, part_counts: {} }));
  });

  it('closes without a request when nothing changed', async () => {
    const save = vi.spyOn(api, 'setLineConfiguration');
    const onClose = vi.fn();
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={line} onClose={onClose} />);
    await screen.findByRole('group', { name: 'Хвіст' });
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    expect(onClose).toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });

  it('keeps a changed count that differs from the configuration', async () => {
    vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue({ reserved_before: 0, reserved_after: 0, finished_before: 0, finished_after: 0, dropping: [] });
    const save = vi.spyOn(api, 'setLineConfiguration').mockResolvedValue({ id: 9, lines: [] } as unknown as Order);
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={line} onClose={() => {}} />);
    fireEvent.change(await screen.findByLabelText('Колба — per unit'), { target: { value: '2' } });
    fireEvent.click(screen.getByLabelText('straight tail — in the kit'));
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(9, 21, { choices: { 3: 11 }, part_counts: { 5: 2, 6: 0 } }));
  });
});
