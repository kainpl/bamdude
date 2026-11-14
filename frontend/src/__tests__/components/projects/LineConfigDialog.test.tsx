/**
 * A line's configuration (spec workshop-product-variants, rules 14, 26–27):
 * the caption under the product, the dialog that changes it — with the
 * server's dry-run impact shown before the save — and the parts-only line.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react';
import i18n from '../../../i18n';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import type { CombinationReason, CombinationState, LineConfigurationBody, Order, Permission, Product, ProjectLine } from '../../../api/client';
import { LineConfigDialog } from '../../../components/projects/LineConfigDialog';
import { lineConfigLabel } from '../../../components/projects/lineConfigLabel';

// The signed-in user's rights pass through, minus what a test denies (`auth.denied`).
const auth = vi.hoisted(() => ({ denied: new Set<string>() }));
vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => {
      const real = actual.useAuth();
      return { ...real, hasPermission: (p: Permission) => !auth.denied.has(p) && real.hasPermission(p) };
    },
  };
});

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

    // A changed choice also waits for the server's word on the combination.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save configuration' })).toBeEnabled());
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
    // Save waits for the dry run's answer — and the combination's.
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save configuration' })).toBeEnabled());
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

/**
 * Which combinations an order can take (spec product-part-variant-combinations п. 25): the
 * server's answer marks, disables and gates; the dialog only says it.
 *
 * The lamp: Tail (3) — straight 11 (standard) / angled 12; Mount (4) — wall 21 (standard) / DIN 22.
 */
const lamp = {
  id: 7,
  name: 'Lamp',
  parts: [
    { id: 5, kind: 'printed', name: 'Shade', qty_per_unit: 1, sort_order: 0, variant_option_ids: [] },
    { id: 6, kind: 'printed', name: 'straight tail', qty_per_unit: 1, sort_order: 1, variant_option_ids: [11] },
    // «And» across groups: only with the angled tail AND the DIN mount.
    { id: 13, kind: 'printed', name: 'angled DIN clip', qty_per_unit: 2, sort_order: 2, variant_option_ids: [12, 22] },
    // «Or» within a group: on either mount.
    { id: 14, kind: 'printed', name: 'mount plate', qty_per_unit: 1, sort_order: 3, variant_option_ids: [21, 22] },
    // Bound to an option the product's groups do not hold (review of Task 6): never «always».
    { id: 15, kind: 'printed', name: 'ghost', qty_per_unit: 3, sort_order: 4, variant_option_ids: [99] },
  ],
  variant_groups: [
    {
      id: 3,
      name: 'Tail',
      position: 0,
      default_option_id: 11,
      options: [
        { id: 11, name: 'straight', position: 0 },
        { id: 12, name: 'angled', position: 1 },
      ],
    },
    {
      id: 4,
      name: 'Mount',
      position: 1,
      default_option_id: 21,
      options: [
        { id: 21, name: 'wall', position: 0 },
        { id: 22, name: 'DIN', position: 1 },
      ],
    },
  ],
} as unknown as Product;

const NAMES: Record<number, string> = { 11: 'straight', 12: 'angled', 21: 'wall', 22: 'DIN' };
const AXES: [number, number[]][] = [
  [3, [11, 12]],
  [4, [21, 22]],
];

/** The server's answer for a choice, when only the combinations in `open` can be ordered. */
function answerFor(open: string[], choices: Record<number, number>): CombinationState {
  const full = { 3: 11, 4: 21, ...choices };
  const keyOf = (c: Record<number, number>) => `${c[3]},${c[4]}`;
  const isOpen = (c: Record<number, number>) => open.includes(keyOf(c));
  return {
    available: isOpen(full),
    reasons: isOpen(full) ? [] : [{ code: 'option_without_part', group: 'Mount', option: NAMES[full[4]], part: null }],
    options: AXES.flatMap(([group, values]) =>
      values.map((value) => ({
        group_id: group,
        option_id: value,
        available_here: isOpen({ ...full, [group]: value }),
        available_anywhere: open.some((k) => k.split(',')[group === 3 ? 0 : 1] === String(value)),
      })),
    ),
  };
}

const lampLine = (tail: number, mount: number) =>
  ({
    id: 21,
    product_id: 7,
    product_name: 'Lamp',
    quantity: 2,
    mode: 'product',
    config_key: `3=${tail},4=${mount}`,
    configuration: {
      choices: [
        { group_id: 3, group_name: 'Tail', option_id: tail, option_name: NAMES[tail], is_default: tail === 11 },
        { group_id: 4, group_name: 'Mount', option_id: mount, option_name: NAMES[mount], is_default: mount === 21 },
      ],
      changed_parts: [],
    },
    from_stock_units: 0,
    parts: [],
  }) as unknown as ProjectLine;

const noImpact = { reserved_before: 0, reserved_after: 0, finished_before: 0, finished_after: 0, dropping: [] };
const CLOSES = 'With the other choices as they are, the combination would not be available';
const NEVER = 'Not in any combination that can be ordered';

/** The server's 409 for a closed combination, with its reasons as name snapshots. */
function refusalOf(reasons: CombinationReason[]): ApiError {
  const refusal = new ApiError('That combination is not available', 409, 'combination_unavailable');
  refusal.reasons = reasons;
  return refusal;
}

/** Longer than the dialog's 300 ms debounce: a dry run it would ask has been asked by now. */
const pastTheDebounce = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 450)));

describe('LineConfigDialog — which combinations an order can take', () => {
  let describeCombination: ReturnType<typeof vi.spyOn>;
  let preview: ReturnType<typeof vi.spyOn>;
  // What the server would answer: the combinations an order may take, and the line's saved one.
  let open: string[] = [];
  let saved = '';
  const openOnly = (...keys: string[]) => {
    open = keys;
  };
  const renderLine = (tail: number, mount: number) => {
    saved = `${tail},${mount}`;
    return render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={lampLine(tail, mount)} onClose={() => {}} />);
  };

  beforeEach(() => {
    vi.restoreAllMocks();
    auth.denied = new Set();
    vi.spyOn(api, 'getProduct').mockResolvedValue(lamp);
    vi.spyOn(api, 'getProductSources').mockResolvedValue({ parts: [] } as never);
    // As the server does (`set_configuration`): a CHANGED closed choice is refused before its dry
    // run; the line's own choice, a parts line and a product without groups are never checked.
    preview = vi
      .spyOn(api, 'previewLineConfiguration')
      .mockImplementation(async (_order: number, _line: number, body: LineConfigurationBody) => {
        const choices = body.choices ?? {};
        if (Object.keys(choices).length === 0) return noImpact;
        const key = `${choices[3]},${choices[4]}`;
        if (key !== saved && !open.includes(key)) throw refusalOf(answerFor(open, choices).reasons);
        return noImpact;
      });
    describeCombination = vi
      .spyOn(api, 'describeCombination')
      .mockImplementation(async (_id: number, choices: Record<number, number>) => answerFor(open, choices));
    openOnly('11,21', '11,22', '12,22');
  });

  /** A value's radio; a group's standard one is named «<value> (standard)». */
  const radio = async (group: string, name: string) =>
    within(await screen.findByRole('group', { name: group })).getByRole('radio', {
      name: new RegExp(`^${name}( \\(standard\\))?$`),
    });
  const saveButton = () => screen.getByRole('button', { name: 'Save configuration' });

  it('marks a value that closes the combination and keeps it clickable', async () => {
    renderLine(11, 21);
    const angled = await radio('Tail', 'angled');
    // angled + wall is closed; angled is in another available combination.
    await waitFor(() => expect(angled).toHaveAccessibleDescription(CLOSES));
    expect(angled).toBeEnabled();
    expect(await radio('Mount', 'DIN')).not.toHaveAccessibleDescription(CLOSES);
    fireEvent.click(angled);
    expect(angled).toBeChecked();
    await waitFor(() => expect(describeCombination).toHaveBeenLastCalledWith(7, { 3: 12, 4: 21 }));
  });

  it('disables a value no available combination contains', async () => {
    openOnly('11,21');
    renderLine(11, 21);
    const din = await radio('Mount', 'DIN');
    await waitFor(() => expect(din).toBeDisabled());
    expect(din).toHaveAccessibleDescription(NEVER);
    expect(await radio('Tail', 'angled')).toBeDisabled();
    // What is chosen is never off.
    expect(await radio('Mount', 'wall')).toBeEnabled();
  });

  it('disables Save on a closed choice and says why — no dry run the server would refuse, nothing to retry', async () => {
    renderLine(11, 21);
    fireEvent.click(await radio('Tail', 'angled'));
    expect(
      await screen.findByText('This combination can’t be ordered: no part for «wall» in «Mount»'),
    ).toBeInTheDocument();
    await pastTheDebounce();
    expect(preview).not.toHaveBeenCalledWith(9, 21, { choices: { 3: 12, 4: 21 }, part_counts: {} });
    expect(screen.queryByText('That combination is not available')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it('keeps Save off while the answer for the new choice is not in, and asks no dry run meanwhile', async () => {
    renderLine(11, 21);
    const din = await radio('Mount', 'DIN');
    await waitFor(() => expect(describeCombination).toHaveBeenCalled());
    describeCombination.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(din);
    expect(await screen.findByText('Checking the combination…')).toBeInTheDocument();
    await pastTheDebounce();
    expect(screen.getByTestId('line-config-impact')).toHaveTextContent('Checking what this changes…');
    expect(preview).not.toHaveBeenCalled();
    expect(saveButton()).toBeDisabled();
  });

  it('lets the operator leave a closed saved choice by changing two groups', async () => {
    // Only straight + DIN is open: from angled + wall, one group alone never gets there.
    openOnly('11,22');
    const save = vi.spyOn(api, 'setLineConfiguration').mockResolvedValue({ id: 9, lines: [] } as unknown as Order);
    renderLine(12, 21);
    const straight = await radio('Tail', 'straight');
    await waitFor(() => expect(straight).toHaveAccessibleDescription(CLOSES));
    expect(straight).toBeEnabled();
    fireEvent.click(straight);
    expect(await screen.findByText(/^This combination can’t be ordered/)).toBeInTheDocument();
    // The closed step between the two: said once, by the note — no refused dry run, no retry.
    await pastTheDebounce();
    expect(screen.queryByText('That combination is not available')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
    // The line's own angled is in no available combination, yet stays choosable — it is the line's.
    expect(await radio('Tail', 'angled')).toBeEnabled();
    const din = await radio('Mount', 'DIN');
    await waitFor(() => expect(din).toHaveAccessibleDescription(''));
    fireEvent.click(din);
    await waitFor(() =>
      expect(screen.getByTestId('line-config-impact')).toHaveTextContent(
        'The reserve will not change; there will be no extra surplus.',
      ),
    );
    await waitFor(() => expect(saveButton()).toBeEnabled());
    expect(screen.queryByTestId('combination-note')).not.toBeInTheDocument();
    expect(preview).toHaveBeenCalledWith(9, 21, { choices: { 3: 11, 4: 22 }, part_counts: {} });
    expect(preview).not.toHaveBeenCalledWith(9, 21, { choices: { 3: 11, 4: 21 }, part_counts: {} });
    fireEvent.click(saveButton());
    await waitFor(() => expect(save).toHaveBeenCalledWith(9, 21, { choices: { 3: 11, 4: 22 }, part_counts: {} }));
  });

  it('without the stock read nothing is asked: the dry run’s refusal alone says why and holds Save', async () => {
    auth.denied = new Set(['stock:read']);
    renderLine(11, 21);
    fireEvent.click(await radio('Tail', 'angled'));
    await waitFor(() => expect(preview).toHaveBeenCalledWith(9, 21, { choices: { 3: 12, 4: 21 }, part_counts: {} }));
    const impact = await screen.findByTestId('line-config-impact');
    await waitFor(() =>
      expect(impact).toHaveTextContent('This combination can’t be ordered: no part for «wall» in «Mount»'),
    );
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
    expect(describeCombination).not.toHaveBeenCalled();
  });

  it('a saved line whose combination closed since still opens, says so, and saves a changed count', async () => {
    openOnly('11,21');
    const save = vi.spyOn(api, 'setLineConfiguration').mockResolvedValue({ id: 9, lines: [] } as unknown as Order);
    renderLine(12, 22);
    expect(await radio('Tail', 'angled')).toBeChecked();
    expect(
      await screen.findByText(
        'This combination can no longer be ordered (no part for «DIN» in «Mount»). The line keeps it; its counts can still be changed.',
      ),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Shade — per unit'), { target: { value: '2' } });
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    await waitFor(() => expect(saveButton()).toBeEnabled());
    fireEvent.click(saveButton());
    await waitFor(() => expect(save).toHaveBeenCalledWith(9, 21, { choices: { 3: 12, 4: 22 }, part_counts: { 5: 2 } }));
  });

  it('shows the 409 combination_unavailable sentence from the server, and asks again', async () => {
    const refusal = refusalOf([{ code: 'part_not_sliced', group: null, option: null, part: 'angled DIN clip' }]);
    vi.spyOn(api, 'setLineConfiguration').mockRejectedValue(refusal);
    renderLine(11, 21);
    fireEvent.click(await radio('Tail', 'angled'));
    fireEvent.click(await radio('Mount', 'DIN'));
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    await waitFor(() => expect(saveButton()).toBeEnabled());
    const asked = describeCombination.mock.calls.length;
    fireEvent.click(saveButton());
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('That combination is not available');
    expect(alert).toHaveTextContent('«angled DIN clip» is not sliced');
    await waitFor(() => expect(describeCombination.mock.calls.length).toBeGreaterThan(asked));
  });

  it('computes the local kit with multi-group bindings', async () => {
    renderLine(11, 21);
    // straight + wall: the clip wants angled AND DIN — out; the plate is on either mount — in.
    expect(await screen.findByLabelText('straight tail — in the kit')).toBeChecked();
    expect(screen.getByLabelText('angled DIN clip — in the kit')).not.toBeChecked();
    expect(screen.getByLabelText('mount plate — in the kit')).toBeChecked();
    fireEvent.click(await radio('Tail', 'angled'));
    // angled + wall: still not the clip's — one group of its two.
    expect(screen.getByLabelText('angled DIN clip — in the kit')).not.toBeChecked();
    fireEvent.click(await radio('Mount', 'DIN'));
    expect(screen.getByLabelText('angled DIN clip — in the kit')).toBeChecked();
    expect(screen.getByLabelText('angled DIN clip — per unit')).toHaveValue(2);
    expect(screen.getByLabelText('mount plate — in the kit')).toBeChecked();
    expect(screen.getByLabelText('straight tail — in the kit')).not.toBeChecked();
  });

  it('never counts a part bound to an option the product does not hold as always in the kit', async () => {
    renderLine(11, 21);
    expect(await screen.findByLabelText('ghost — in the kit')).not.toBeChecked();
    expect(screen.getByLabelText('ghost — per unit')).toHaveValue(0);
  });

  it('asks nothing for a parts line or a product without groups', async () => {
    const partsLine = { ...lampLine(11, 21), mode: 'parts', configuration: { choices: [], changed_parts: [] } } as ProjectLine;
    const first = render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={partsLine} onClose={() => {}} />);
    expect(await screen.findByLabelText('Shade — needed')).toBeInTheDocument();
    first.unmount();
    vi.spyOn(api, 'getProduct').mockResolvedValue({ ...lamp, variant_groups: [] } as unknown as Product);
    const plain = { ...lampLine(11, 21), configuration: { choices: [], changed_parts: [] } } as ProjectLine;
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={plain} onClose={() => {}} />);
    expect(await screen.findByText('The product has no variants — only its parts can be changed.')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Shade — per unit'), { target: { value: '2' } });
    await screen.findByText('The reserve will not change; there will be no extra surplus.');
    await waitFor(() => expect(saveButton()).toBeEnabled());
    expect(describeCombination).not.toHaveBeenCalled();
  });
});
