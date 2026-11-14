/**
 * WS-13 E5 F — «Part configuration» as the mockup's `renderConfig` draws it: the
 * Workshop frame naming the order and the line, radio groups, the five-column table
 * with sources, the standard and its full reset (R03), the product's own read states
 * and the rule for when a changed draft may be saved (R05), a group without a
 * standard option left unchosen (R11).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { QueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import type { Order, Product, ProjectLine } from '../../../api/client';
import { LineConfigDialog } from '../../../components/projects/LineConfigDialog';
import { saveAllowed } from '../../../components/projects/lineConfigSave';

const product = {
  id: 7,
  name: 'Pipe',
  parts: [
    { id: 5, kind: 'printed', name: 'Колба', qty_per_unit: 1, sort_order: 0, variant_option_ids: [] },
    { id: 6, kind: 'printed', name: 'straight tail', qty_per_unit: 1, sort_order: 1, variant_option_ids: [11] },
    { id: 8, kind: 'printed', name: 'angled tail', qty_per_unit: 1, sort_order: 2, variant_option_ids: [12] },
    { id: 10, kind: 'purchased', name: 'screw', qty_per_unit: 2, sort_order: 3, variant_option_ids: [] },
    { id: 9, kind: 'printed', name: 'test cube', qty_per_unit: 0, sort_order: 4, variant_option_ids: [], ignored: true },
  ],
  variant_groups: [
    {
      id: 3,
      name: 'Хвіст',
      position: 0,
      default_option_id: 11,
      options: [
        { id: 11, name: 'прямий', position: 0 },
        { id: 12, name: 'кутовий', position: 1 },
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
  parts: [],
} as unknown as ProjectLine;

const src = (plate_index: number, printer_model: string, y: number, recommended = false) => ({
  plate_id: plate_index,
  library_file_id: 1,
  filename: 'f.gcode.3mf',
  folder_id: null,
  folder_name: null,
  hidden: false,
  plate_index,
  printer_model,
  sliced: true,
  yield: y,
  print_time_seconds: 60,
  filament_used_grams: 1,
  recommended,
});

const noImpact = { reserved_before: 0, reserved_after: 0, finished_before: 0, finished_after: 0, dropping: [] };

describe('LineConfigDialog (WS-13 E5 F)', () => {
  let preview: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(product);
    vi.spyOn(api, 'getProductSources').mockResolvedValue({
      parts: [
        { part_id: 5, sources: [src(2, 'X1C', 6, true), src(1, 'P1S', 4), src(3, 'A1', 2)], has_sliced_source: true, yield_min: 2, yield_max: 6, hidden_sources: 0 },
        { part_id: 6, sources: [], has_sliced_source: false, yield_min: null, yield_max: null, hidden_sources: 0 },
      ],
    } as never);
    preview = vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue(noImpact);
  });

  const open = (l: ProjectLine = line, onClose = () => {}) =>
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={l} onClose={onClose} />);

  it('is a Workshop lg dialog naming the order and the line', async () => {
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Part configuration' });
    expect(dialog).toHaveStyle({ maxWidth: 'min(1000px, 94vw)' });
    expect(dialog).toHaveAccessibleDescription('OR-0009 · Pipe × 4');
  });

  it('offers each group as radios, the standard option marked', async () => {
    open();
    const group = await screen.findByRole('group', { name: 'Хвіст' });
    expect(within(group).getByRole('radio', { name: /прямий/ })).toBeChecked();
    expect(within(group).getByRole('radio', { name: /прямий/ })).toHaveAccessibleName('прямий (standard)');
    expect(within(group).getByRole('radio', { name: 'кутовий' })).not.toBeChecked();
    expect(screen.queryByRole('combobox', { name: 'Хвіст' })).not.toBeInTheDocument();
  });

  it('heads its table and draws every counted part, other options’ parts too', async () => {
    open();
    await screen.findByRole('group', { name: 'Хвіст' });
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'In the kit',
      'Part',
      'Per unit',
      'Total for 4',
      'Source',
    ]);
    expect(within(table).queryByText('test cube')).not.toBeInTheDocument();

    const flask = within(screen.getByTestId('config-part-5'));
    expect(flask.getByLabelText('Колба — in the kit')).toBeChecked();
    expect(flask.getByLabelText('Колба — per unit')).toHaveValue(1);
    expect(flask.getByText('standard 1')).toBeInTheDocument();
    expect(flask.getByText('4')).toBeInTheDocument();

    const angled = within(screen.getByTestId('config-part-8'));
    expect(angled.getByLabelText('angled tail — in the kit')).not.toBeChecked();
    expect(angled.getByText('variant: кутовий')).toBeInTheDocument();
    expect(angled.getByText('not in the standard')).toBeInTheDocument();
    expect(angled.getByText('—')).toBeInTheDocument();

    const screw = within(screen.getByTestId('config-part-10'));
    expect(screw.getByText('bought')).toBeInTheDocument();
    expect(screw.getByText('purchase')).toBeInTheDocument();
    expect(screw.getByText('8')).toBeInTheDocument();
  });

  it('shows up to two sources, the recommended first, and says when there is none', async () => {
    open();
    const flask = within(await screen.findByTestId('config-part-5'));
    expect(await flask.findByText('pl. 2 × 6')).toBeInTheDocument();
    expect(flask.getByText('pl. 1 × 4')).toBeInTheDocument();
    expect(flask.queryByText('pl. 3 × 2')).not.toBeInTheDocument();
    expect(within(screen.getByTestId('config-part-6')).getByText('no plate')).toBeInTheDocument();
  });

  it('shows two sources a reader can tell apart, not one repeated (WS-13 E5 T7)', async () => {
    // Two files of one model, same plate and yield, read the same: the second says nothing.
    vi.spyOn(api, 'getProductSources').mockResolvedValue({
      parts: [
        { part_id: 5, sources: [src(2, 'X1C', 6, true), src(2, 'X1C', 6), src(1, 'P1S', 4)], has_sliced_source: true, yield_min: 4, yield_max: 6, hidden_sources: 0 },
      ],
    } as never);
    open();
    const flask = within(await screen.findByTestId('config-part-5'));
    expect(await flask.findByText('pl. 1 × 4')).toBeInTheDocument();
    expect(flask.getAllByText('pl. 2 × 6')).toHaveLength(1);
  });

  it('shows only sliced sources: a part with an unsliced file alone has no plate (review I2)', async () => {
    // An unsliced 3MF is a source the plan cannot print from, whatever model it names.
    const unsliced = (n: number, model: string, y: number) => ({ ...src(n, model, y), plate_id: 100 + n, sliced: false });
    vi.spyOn(api, 'getProductSources').mockResolvedValue({
      parts: [
        { part_id: 5, sources: [src(2, 'X1C', 6, true), unsliced(2, 'X1C', 6)], has_sliced_source: true, yield_min: 6, yield_max: 6, hidden_sources: 0 },
        { part_id: 6, sources: [unsliced(1, 'P1S', 4)], has_sliced_source: false, yield_min: null, yield_max: null, hidden_sources: 0 },
      ],
    } as never);
    open();
    const flask = within(await screen.findByTestId('config-part-5'));
    expect(await flask.findByText('pl. 2 × 6')).toBeInTheDocument();
    expect(flask.getAllByText(/^pl\. /)).toHaveLength(1);
    const cap = within(screen.getByTestId('config-part-6'));
    expect(await cap.findByText('no plate')).toBeInTheDocument();
    expect(cap.queryByText('pl. 1 × 4')).not.toBeInTheDocument();
  });

  it('marks as changed only the counts that will be saved (review M2)', async () => {
    // Untick the straight tail (an override of 0), then choose the angled tail, which gives
    // it 0 anyway: nothing is overridden any more, and the dialog must not say otherwise.
    open();
    const straight = within(await screen.findByTestId('config-part-6'));
    fireEvent.click(straight.getByLabelText('straight tail — in the kit'));
    expect(await straight.findByText('changed')).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    await waitFor(() => expect(straight.queryByText('changed')).not.toBeInTheDocument());
    expect(screen.getByText('The kit follows the chosen variants.')).toBeInTheDocument();
  });

  it('disables Save when the re-read of the same preview fails, and enables it after a good retry (R05, review M6)', async () => {
    // The wiring, not only `saveAllowed`: an answer stays cached while its re-read fails.
    const held: { client: QueryClient | null } = { client: null };
    function Grab() {
      const client = useQueryClient();
      useEffect(() => {
        held.client = client;
      }, [client]);
      return null;
    }
    preview
      .mockResolvedValueOnce(noImpact)
      .mockRejectedValueOnce(new ApiError('Could not check', 500))
      .mockResolvedValue(noImpact);
    render(
      <>
        <LineConfigDialog orderId={9} orderCode="OR-0009" line={line} onClose={() => {}} />
        <Grab />
      </>,
    );
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    const save = screen.getByRole('button', { name: 'Save configuration' });
    await waitFor(() => expect(save).toBeEnabled());
    await act(async () => {
      await held.client!.refetchQueries({ queryKey: ['line-config-preview'] });
    });
    await waitFor(() => expect(save).toBeDisabled());
    fireEvent.click(within(screen.getByTestId('line-config-impact')).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(save).toBeEnabled());
  });

  it('does not wait on the sources: a failed read leaves a dash', async () => {
    vi.spyOn(api, 'getProductSources').mockRejectedValue(new ApiError('boom', 500));
    open();
    const flask = within(await screen.findByTestId('config-part-5'));
    expect(await flask.findByTitle('Could not read the sources')).toHaveTextContent('—');
  });

  it('marks a changed count and says what the draft is against the standard (R03)', async () => {
    open();
    const group = await screen.findByRole('group', { name: 'Хвіст' });
    expect(
      screen.getByText('The product’s standard configuration. A tick adds or removes a part in this line only — the product itself does not change.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reset to standard' })).not.toBeInTheDocument();

    fireEvent.click(within(group).getByRole('radio', { name: 'кутовий' }));
    expect(screen.getByText('The kit follows the chosen variants.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reset to standard' })).toHaveAttribute('title', 'Standard variants and counts');

    fireEvent.change(screen.getByLabelText('Колба — per unit'), { target: { value: '2' } });
    expect(screen.getByText('Counts changed against the chosen variants: 1.')).toBeInTheDocument();
    expect(within(screen.getByTestId('config-part-5')).getByText('changed')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Reset to standard' }));
    expect(within(group).getByRole('radio', { name: /прямий/ })).toBeChecked();
    expect(screen.getByLabelText('Колба — per unit')).toHaveValue(1);
  });

  it('a saved non-standard line offers the reset at once, and it sends the standard (R03)', async () => {
    const save = vi.spyOn(api, 'setLineConfiguration').mockResolvedValue({ id: 9, lines: [] } as unknown as Order);
    const angled = {
      ...line,
      configuration: {
        choices: [{ group_id: 3, group_name: 'Хвіст', option_id: 12, option_name: 'кутовий', is_default: false }],
        changed_parts: [{ part_id: 5, name: 'Колба', qty: 3, standard_qty: 1 }],
      },
    } as ProjectLine;
    open(angled);
    await screen.findByRole('group', { name: 'Хвіст' });
    expect(screen.getByText('Counts changed against the chosen variants: 1.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reset to standard' }));
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(9, 21, { choices: { 3: 11 }, part_counts: {} }));
  });

  it('names each part that would become surplus, and says a quiet change plainly', async () => {
    preview.mockResolvedValue({
      reserved_before: 4,
      reserved_after: 2,
      finished_before: 2,
      finished_after: 0,
      dropping: [{ part_id: 6, name: 'straight tail', per_before: 1, per_after: 0, printed: 3, queued: 1 }],
    });
    open();
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    expect(await screen.findByText('Kit reserve: 4 → 2')).toBeInTheDocument();
    expect(screen.getByText('Ready from stock: 2 → 0')).toBeInTheDocument();
    expect(screen.getByText('straight tail: 3 printed, 1 queued will become surplus')).toBeInTheDocument();
  });

  it('a larger count that moves nothing says the reserve will not change (R10)', async () => {
    open();
    fireEvent.change(await screen.findByLabelText('Колба — per unit'), { target: { value: '3' } });
    expect(await screen.findByText('The reserve will not change; there will be no extra surplus.')).toBeInTheDocument();
  });

  it('a failed impact says so, offers to ask again, and keeps Save off until it answers (R05)', async () => {
    preview.mockRejectedValueOnce(new ApiError('Could not check', 500));
    open();
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    expect(await screen.findByText('Could not check')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save configuration' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    expect(screen.getByRole('button', { name: 'Save configuration' })).toBeEnabled();
  });

  it('says a failed save inside the dialog and keeps the draft', async () => {
    vi.spyOn(api, 'setLineConfiguration').mockRejectedValue(new ApiError('This line’s stock has moved', 409));
    open();
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('This line’s stock has moved');
    expect(within(screen.getByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' })).toBeChecked();
  });

  it('reads the product: loading, a failed read with retry, and a product with no parts (R05)', async () => {
    const get = vi.spyOn(api, 'getProduct').mockRejectedValue(new ApiError('Product not found', 404));
    const onClose = vi.fn();
    open(line, onClose);
    expect(await screen.findByText('Could not read the product')).toBeInTheDocument();
    expect(screen.getByText('Product not found')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save configuration' })).toBeDisabled();
    get.mockResolvedValue({ ...product, parts: [], variant_groups: [] } as unknown as Product);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('The product has no parts that can be configured.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save configuration' }));
    expect(onClose).toHaveBeenCalled();
  });

  it('a parts-only line: three columns, no ticks, named as parts only', async () => {
    const parts = {
      ...line,
      mode: 'parts',
      configuration: { choices: [], changed_parts: [{ part_id: 5, name: 'Колба', qty: 3, standard_qty: 0 }] },
    } as unknown as ProjectLine;
    open(parts);
    const dialog = await screen.findByRole('dialog', { name: 'Part configuration' });
    expect(dialog).toHaveAccessibleDescription('OR-0009 · Pipe · parts only');
    const table = await screen.findByRole('table');
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Part', 'Needed, pcs', 'Source']);
    expect(within(table).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Колба — needed')).toHaveValue(3);
    expect(screen.queryByRole('group', { name: 'Хвіст' })).not.toBeInTheDocument();
  });
});

describe('LineConfigDialog — a group without a standard option (WS-13 E5 R11)', () => {
  const mounted = {
    id: 8,
    name: 'Case',
    parts: [
      { id: 31, kind: 'printed', name: 'wall mount', qty_per_unit: 1, sort_order: 0, variant_option_ids: [21] },
      { id: 32, kind: 'printed', name: 'DIN clip', qty_per_unit: 1, sort_order: 1, variant_option_ids: [22] },
    ],
    variant_groups: [
      { id: 4, name: 'Кріплення', position: 0, default_option_id: null, options: [{ id: 21, name: 'стіна', position: 0 }, { id: 22, name: 'DIN', position: 1 }] },
    ],
  } as unknown as Product;
  const bare = {
    id: 22,
    product_id: 8,
    product_name: 'Case',
    quantity: 2,
    mode: 'product',
    config_key: '',
    configuration: { choices: [], changed_parts: [] },
    parts: [],
  } as unknown as ProjectLine;

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(mounted);
    vi.spyOn(api, 'getProductSources').mockResolvedValue({ parts: [] } as never);
  });

  it('shows «No choice» chosen, asks with and without the option, and a reset invents none', async () => {
    const preview = vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue(noImpact);
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={bare} onClose={() => {}} />);
    const group = await screen.findByRole('group', { name: 'Кріплення' });
    expect(within(group).getByRole('radio', { name: 'No choice' })).toBeChecked();

    fireEvent.click(within(group).getByRole('radio', { name: 'DIN' }));
    await waitFor(() => expect(preview).toHaveBeenLastCalledWith(9, 22, { choices: { 4: 22 }, part_counts: {} }));

    fireEvent.click(screen.getByRole('button', { name: 'Reset to standard' }));
    expect(within(group).getByRole('radio', { name: 'No choice' })).toBeChecked();
  });
});

describe('saveAllowed (WS-13 E5 R05)', () => {
  const base = { productRead: true, dirty: true, settled: true, fetching: false, status: 'success' as const };

  it('allows a settled, answered change', () => {
    expect(saveAllowed(base)).toBe(true);
  });

  it('allows an unchanged draft — Save only closes', () => {
    expect(saveAllowed({ ...base, dirty: false, status: 'pending' })).toBe(true);
  });

  it('refuses without the product, while the draft settles, while asking, and after a failed ask', () => {
    expect(saveAllowed({ ...base, productRead: false })).toBe(false);
    expect(saveAllowed({ ...base, settled: false })).toBe(false);
    expect(saveAllowed({ ...base, fetching: true })).toBe(false);
    // A cached answer after a failed re-read is not an answer for this draft.
    expect(saveAllowed({ ...base, status: 'error' })).toBe(false);
    expect(saveAllowed({ ...base, status: 'pending' })).toBe(false);
  });
});
