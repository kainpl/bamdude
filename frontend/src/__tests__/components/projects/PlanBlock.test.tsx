import { mockWorkingPlanApi } from '../../fixtures/workingPlan';
/**
 * The plan block is the only place in the app that does arithmetic on order
 * data client-side, so the two things it must not get wrong are covered here:
 * the what-if follows the PLATE'S yields (not the row's clipped `useful`), and
 * "whole plan to queue" sends exactly the rows the operator left standing.
 *
 * ⚠️ The fixture is built so all three possible sources of the surplus figure
 * disagree. The server says 3; row 100's `useful` is 10 and row 200's is 2,
 * summing to 12 against a need of 12, i.e. 0; the plate recipes yield 10 and 6,
 * which is 4. Only a projection that reads the RECIPES can show 4 — and it must
 * show 4 at the very counts the server planned, before anything is edited.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within, cleanup, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { QueryClientProvider, QueryClient, onlineManager, useQuery, useQueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import { api, ApiError } from '../../../api/client';
import type {
  Order,
  OrderForecastDetail,
  OrderNeeds,
  OrderPlan,
  PlanAlternative,
  PlanRow as PlanRowData,
  Permission,
  PlateRecipe,
  Printer,
} from '../../../api/client';
import { DEFAULT_PRINT_OPTIONS } from '../../../components/PrintModal/types';
import { PlanBlock } from '../../../components/projects/PlanBlock';
import { ORDER_VIEW_KEYS } from '../../../utils/queryInvalidation';
import { FORECAST_DEFAULTS } from '../../wireDefaults';

/** What the mocked `useAuth` grants — reset in `beforeEach`, narrowed per test. */
const auth = vi.hoisted(() => ({ granted: new Set<string>() }));

/** The printer leg is asserted on its PROPS. What "to printer…" hands the modal
 *  — the file, the plate, the order, the line and the locked dispatch mode — is
 *  the whole of the contract between the plan and the printer; rendering the
 *  real modal would test the modal instead, and it is a heavy tree that fetches
 *  half the farm. */
const printModal = vi.hoisted(() => ({ props: null as Record<string, unknown> | null }));

vi.mock('../../../components/PrintModal', () => ({
  PrintModal: (props: Record<string, unknown>) => {
    printModal.props = props;
    return null;
  },
}));

// The block gates its two row actions on two DIFFERENT permissions, and the
// real `AuthProvider` in the render helper always resolves the same admin. Only
// the hook is replaced; everything else (the provider itself, above all) is the
// real module, so the tree still mounts the way the app mounts it.
vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({ ...actual.useAuth(), hasPermission: (p: Permission) => auth.granted.has(p) }),
  };
});

/** A stand-in for `OrderPage`'s own `['project', id]` query — an invalidation
 *  is observable only as a refetch, and only while something watches the key. */
function OrderProbe({ id, onFetch }: { id: number; onFetch: () => void }) {
  useQuery({
    queryKey: ['project', id],
    queryFn: async () => {
      onFetch();
      return null;
    },
  });
  return null;
}

/** A refetch of the plan the operator did not ask for — a print finishing on
 *  another printer invalidates `project-plan` farm-wide (the archive events
 *  carry no `project_id`), and this is that event with nothing else attached. */
function Refetcher({ id }: { id: number }) {
  const queryClient = useQueryClient();
  return (
    <button
      type="button"
      data-testid="force-refetch"
      onClick={() => queryClient.invalidateQueries({ queryKey: ['project-plan', id] })}
    >
      refetch
    </button>
  );
}

/** A refetch of the printer list — a printer archived or parked meanwhile. */
function PrintersRefetcher() {
  const queryClient = useQueryClient();
  return (
    <button
      type="button"
      data-testid="force-printers-refetch"
      onClick={() => queryClient.invalidateQueries({ queryKey: ['printers'] })}
    >
      refetch printers
    </button>
  );
}

const order = {
  id: 1,
  name: 'Ten flasks',
  status: 'active',
  lines: [
    {
      id: 10,
      product_id: 7,
      product_name: 'Flask',
      quantity: 12,
      material: 'PETG',
      color: null,
      note: null,
      sort_order: 0,
      units_printed: 0,
      covered_units: 0,
      progress: 0,
      archive_ids: [],
      parts: [
        { part_id: 1, name: 'Body', qty_per_unit: 1, need: 12, usable: 0, in_progress: 0, remaining: 12, surplus: 0 },
        { part_id: 2, name: 'Cap', qty_per_unit: 1, need: 12, usable: 9, in_progress: 0, remaining: 3, surplus: 0 },
      ],
    },
  ],
} as unknown as Order;

const plan: OrderPlan = {
  lines: [
    {
      line_id: 10,
      product_id: 7,
      product_name: 'Flask',
      material: 'PETG',
      outstanding_before: [{ part_id: 1, name: 'Body', count: 12 }],
      rows: [
        {
          plate_id: 100,
          library_file_id: 5,
          plate_index: 1,
          filename: 'big.3mf',
          hidden: false,
          count: 1,
          useful: [{ part_id: 1, name: 'Body', count: 10 }],
          print_time_seconds: 3600,
          filament_used_grams: 100,
          cost: 2,
          time_unknown: false,
          printer_model: 'X1C',
          alternatives: [],
        },
        {
          plate_id: 200,
          library_file_id: 6,
          plate_index: 0,
          filename: 'small.3mf',
          hidden: false,
          count: 1,
          useful: [{ part_id: 1, name: 'Body', count: 2 }],
          print_time_seconds: 1800,
          filament_used_grams: 20,
          cost: 0.4,
          time_unknown: false,
          printer_model: null,
          alternatives: [],
        },
      ],
      // The server's own answer, and deliberately NOT what the recipes below
      // make of the same counts — see the file header.
      surplus_after: [{ part_id: 1, name: 'Body', count: 3 }],
      unsatisfiable: [{ part_id: 2, name: 'Cap', count: 3 }],
      candidates: [100, 200],
      not_sliced: [],
    },
  ],
  totals: { rows: 0, prints: 2, print_time_seconds: 5400, filament_used_grams: 120, cost: 2.4 },
};

const plates: PlateRecipe[] = [
  {
    id: 100,
    library_file_id: 5,
    plate_index: 1,
    filename: 'big.3mf',
    hidden: false,
    sliced: true,
    yield: [{ part_id: 1, name: 'Body', count: 10 }],
    unassigned: [],
    materials: ['PETG'],
    colors: [],
    printer_model: null,
    print_time_seconds: 3600,
    filament_used_grams: 100,
  },
  {
    id: 200,
    library_file_id: 6,
    plate_index: 0,
    filename: 'small.3mf',
    hidden: false,
    sliced: true,
    yield: [{ part_id: 1, name: 'Body', count: 6 }],
    unassigned: [],
    materials: ['PETG'],
    colors: [],
    printer_model: null,
    print_time_seconds: 1800,
    filament_used_grams: 20,
  },
];

/** A candidate the greedy did NOT pick — the only shape in which the "+ plate"
 *  menu has anything to offer. Priced nowhere on the wire: its cost has to be
 *  derived from the rate a costed server row implies (2.00 for 100 g). */
const spare: PlateRecipe = {
  id: 300,
  library_file_id: 9,
  plate_index: 2,
  filename: 'extra.3mf',
  hidden: false,
  sliced: true,
  yield: [{ part_id: 1, name: 'Body', count: 3 }],
  unassigned: [],
  materials: ['PETG'],
  colors: [],
  printer_model: null,
  print_time_seconds: 900,
  filament_used_grams: 50,
};

const planWithSpare: OrderPlan = {
  ...plan,
  lines: [{ ...plan.lines[0], candidates: [100, 200, 300] }],
};

/** The same spare plate, once the server has planned it itself. */
const spareRow: PlanRowData = {
  plate_id: 300,
  library_file_id: 9,
  plate_index: 2,
  filename: 'extra.3mf',
  hidden: false,
  count: 2,
  useful: [{ part_id: 1, name: 'Body', count: 3 }],
  print_time_seconds: 900,
  filament_used_grams: 50,
  cost: 1,
  time_unknown: false,
  printer_model: null,
  alternatives: [],
};

/** Two lines of the SAME product, so one `getProductPlates` mock serves both
 *  and the only difference between them is which line the operator edited. */
const twoLineOrder = {
  ...order,
  lines: [order.lines[0], { ...order.lines[0], id: 20, sort_order: 1 }],
} as unknown as Order;

const twoLinePlan: OrderPlan = {
  ...plan,
  lines: [plan.lines[0], { ...plan.lines[0], line_id: 20 }],
};

/** The same ten bodies, sliced for the other machine — a different FILE with
 *  the identical counted yield, which is the whole shape this feature is
 *  about. Slower and heavier, so switching to it is visible in every figure. */
const otherModel: PlanAlternative = {
  plate_id: 400,
  library_file_id: 8,
  plate_index: 2,
  filename: 'big-p1s.3mf',
  hidden: false,
  printer_model: 'P1S',
  print_time_seconds: 7200,
  filament_used_grams: 150,
  cost: 3,
  time_unknown: false,
};

const planWithAlternative: OrderPlan = {
  ...plan,
  lines: [
    {
      ...plan.lines[0],
      candidates: [100, 200, 400],
      rows: [{ ...plan.lines[0].rows[0], alternatives: [otherModel] }, plan.lines[0].rows[1]],
    },
  ],
};

/** The alternative as the product's plate list has it — what "+ plate" would
 *  offer if nothing stopped it. */
const alternativePlate: PlateRecipe = {
  ...plates[0],
  id: 400,
  library_file_id: 8,
  plate_index: 2,
  filename: 'big-p1s.3mf',
};

const farm = [
  { id: 1, name: 'Carbon', model: 'X1C', is_active: true },
  { id: 2, name: 'Pea', model: 'P1S', is_active: true },
] as unknown as Printer[];

/** No line answers yet — the shape every test gets unless it asks for a
 *  specific forecast. */
const EMPTY_FORECAST: OrderForecastDetail = {
  ...FORECAST_DEFAULTS, by_model: [],
  project_id: order.id, now_eta: null, now_seconds: null, after_eta: null, after_seconds: null, machine_seconds: 0,
  unknown_prints: 0, unroutable_prints: 0, eta_complete: true, ahead_count: 0, assumptions: [], lines: [],
};

/** The farm's answer for line 10: row 100's print on the P1S file. */
const proposing: OrderForecastDetail = {
  ...EMPTY_FORECAST,
  lines: [
    {
      line_id: 10,
      now_eta: '2026-09-07T10:00:00Z',
      now_seconds: 7200,
      after_eta: null,
      after_seconds: null,
      unknown_prints: 0,
      unroutable_prints: 0,
      eta_complete: true,
      rows: [{ plate_id: 100, proposed_split: { 100: 0, 400: 1 } }],
    },
  ],
};

/** No filament need reported yet — the shape every test gets unless it asks
 *  for a specific need-vs-shelf response. */
const EMPTY_NEEDS: OrderNeeds = { project_id: order.id, rows: [], unknown_prints: 0, stock_unavailable: false, assumptions: ['slicer_estimate'] };

describe('PlanBlock', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    const readWorking = mockWorkingPlanApi();
    printModal.props = null;
    // The product's files are a link only for whoever may open the product (WS-13 E13 O19).
    auth.granted = new Set(['orders:update', 'products:read', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'queue:create', 'printers:control']);
    vi.spyOn(api, 'getOrderPlan').mockImplementation(async () => {
      const state = readWorking();
      return state.revision === 0 ? plan : { ...plan, mode: 'working', working_revision: state.revision,
        inputs_token: state.inputs_token, lines: plan.lines.map(line => ({ ...line,
          rows: line.rows.flatMap(row => {
            const entry = state.entries.find(e => e.line_id === line.line_id && e.plate_id === row.plate_id);
            return entry ? [{ ...row, count: entry.count, entry_id: entry.entry_id }] : [];
          }) })) };
    });
    vi.spyOn(api, 'getOrderPrintOptions').mockResolvedValue({ overrides: {}, models: [] });
    vi.spyOn(api, 'getProductPlates').mockResolvedValue(plates);
    // The currency is the test's choice, not `formatMoney`'s USD fallback —
    // otherwise the money assertions pass on an unresolved settings query.
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ currency: 'UAH' } as never);
    // A row with alternatives asks which printers exist, so "to printer…" can
    // hand the dialog the file that machine was sliced for. A plan without one
    // asks nothing — the query is gated — and this mock covers both.
    vi.spyOn(api, 'getPrinters').mockResolvedValue(farm);
    // The model matrix «To printer» filters by — read, and empty unless a test says otherwise.
    vi.spyOn(api, 'getModelCompatibility').mockResolvedValue({ models: {} });
    vi.spyOn(api, 'getOrderForecast').mockResolvedValue(EMPTY_FORECAST);
    vi.spyOn(api, 'getOrderFilament').mockResolvedValue(EMPTY_NEEDS);
  });

  it.each(['success', 'failed', 'focus'])('confirmed recovery preserves edits: %s', async mode => {
    const state = { ...await api.getWorkingPlan(1), revision: 1,
      entries: [{ entry_id: 'row-10-100', line_id: 10, plate_id: 100, count: 20,
        target: { kind: 'auto' as const }, manual: true, protected_count: 20 }] };
    vi.mocked(api.getWorkingPlan).mockResolvedValue(state);
    vi.mocked(api.getOrderPlan).mockResolvedValue({ ...plan, working_revision: 1, inputs_token: state.inputs_token });
    vi.mocked(api.writeWorkingPlan).mockRejectedValue(new ApiError('Inputs changed', 409, 'inputs_changed'));
    render(<PlanBlock order={order} canEdit />);
    const input = await screen.findByTestId('plan-row-10-100-count');
    fireEvent.change(input, { target: { value: '18' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Read shared plan again' }));
    const dialog = await screen.findByRole('dialog');
    expect(input).toHaveValue(18);
    let finish: (() => void) | undefined;
    if (mode === 'failed') vi.mocked(api.getWorkingPlan).mockRejectedValue(new Error('workingPlan.conflict'));
    if (mode === 'focus') vi.mocked(api.getWorkingPlan).mockImplementationOnce(async () => {
      await new Promise<void>(resolve => { finish = resolve; });
      return { ...state, revision: 2, entries: [{ ...state.entries[0], count: 14 }] };
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Read shared plan again' }));
    if (mode === 'focus') {
      await waitFor(() => expect(finish).toBeDefined());
      const calls = vi.mocked(api.getWorkingPlan).mock.calls.length;
      fireEvent.focus(window);
      expect(api.getWorkingPlan).toHaveBeenCalledTimes(calls);
      await act(async () => { finish!(); });
    }
    if (mode === 'failed') {
      await within(dialog).findByText(/Could not save. Your edits are still here/);
      expect(input).toHaveValue(18);
      expect(dialog).not.toHaveTextContent('workingPlan.conflict');
    } else {
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(input).toHaveValue(mode === 'focus' ? 14 : 20);
      expect(input).not.toBeDisabled();
      expect(screen.queryByText('Local changes are not saved yet')).toBeNull();
    }
    expect(api.writeWorkingPlan).toHaveBeenCalledTimes(1);
  });

  it('shows the server rebuild summary and discards a stale proposal with its dialog', async () => {
    const save = vi.spyOn(api, 'actWorkingPlan');
    const replan = vi.spyOn(api, 'replanWorkingPlan').mockResolvedValue({
      proposal_token: 'reviewed', inputs_token: 'inputs', entries: [], previous_entries: [],
      previous_count: 2, count: 0, removed_issue_entry_count: 17,
    });
    render(<PlanBlock order={order} canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: 'Rebuild whole draft' }));
    const proposal = await screen.findByTestId('working-plan-proposal');
    expect(proposal).toHaveTextContent('Problem entries removed or replaced: 17');
    expect(proposal).toHaveTextContent('Not applied yet');
    fireEvent.click(within(proposal).getByRole('button', { name: 'Apply to draft' }));
    const dialog = await screen.findByRole('dialog');
    replan.mockRejectedValue(new ApiError('Changed', 409, 'inputs_changed'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply to draft' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByTestId('working-plan-proposal')).toBeNull();
    expect(screen.getByText(/Calculate a new proposal/)).toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
    expect(api.registerPlanSubmission).not.toHaveBeenCalled();
  });

  it('replaces acknowledged local overlays with a foreign working revision even while metadata still has old counts', async () => {
    let state: import('../../../api/client').WorkingPlan = { ...await api.getWorkingPlan(1), revision: 1,
      entries: [{ entry_id: 'row-10-100', line_id: 10, plate_id: 100, count: 20,
        target: { kind: 'auto' as const }, manual: true, protected_count: 20 }] };
    vi.mocked(api.getWorkingPlan).mockImplementation(async () => structuredClone(state));
    vi.mocked(api.writeWorkingPlan).mockImplementation(async (_, body) => {
      state = { ...state, revision: state.revision + 1, entries: body.entries };
      return structuredClone(state);
    });
    vi.mocked(api.getOrderPlan).mockImplementation(async () => ({ ...plan, working_revision: 1 }));
    render(<PlanBlock order={order} canEdit />);
    fireEvent.change(await screen.findByTestId('plan-row-10-100-count'), { target: { value: '18' } });
    await waitFor(() => expect(state.entries[0].count).toBe(18));
    state = { ...state, revision: state.revision + 1, entries: [{ ...state.entries[0], count: 14, protected_count: 14 }] };
    fireEvent.focus(window);
    await waitFor(() => expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(14));
    expect(api.writeWorkingPlan).toHaveBeenCalledTimes(1);
    expect(state.entries[0].count).toBe(14);
  });

  it.each(['stalled', 'failed'])('never republishes consumed entries while the old context refetch is %s', async (outcome) => {
    let state: import('../../../api/client').WorkingPlan = { schema_version: 1, revision: 1,
      entries: plan.lines.flatMap(line => line.rows.map(row => ({ entry_id: `row-${line.line_id}-${row.plate_id}`,
        line_id: line.line_id, plate_id: row.plate_id, count: row.count, target: { kind: 'auto' as const },
        manual: false, protected_count: 0 }))),
      saved_revision: 0, saved_at: null, saved_by: null, epoch: 0, saved_epoch: null,
      active_operation: null, inputs_token: 'inputs', changed: false, issues: [] };
    const queuedIds: number[] = [];
    let operation: import('../../../api/client').PlanSubmission = { operation_id: '', status: 'pending', created: [], remaining: [] };
    vi.mocked(api.getWorkingPlan).mockImplementation(async () => {
      if (vi.mocked(api.getWorkingPlan).mock.calls.length > 12) throw new Error('unbounded context read');
      return structuredClone(state);
    });
    vi.mocked(api.writeWorkingPlan).mockImplementation(async (_, body) => {
      if (body.expected_revision !== state.revision) throw new ApiError('revision changed', 409);
      state = { ...state, revision: state.revision + 1, entries: structuredClone(body.entries) };
      return structuredClone(state);
    });
    vi.mocked(api.registerPlanSubmission).mockImplementation(async (_, body) => {
      expect(body.expected_revision).toBe(state.revision);
      state = { ...state, active_operation: body.operation_id };
      operation = { operation_id: body.operation_id, status: 'pending', created: [], remaining: body.parts };
      return structuredClone(operation);
    });
    vi.mocked(api.getPlanSubmission).mockImplementation(async () => structuredClone(operation));
    vi.mocked(api.actPlanSubmission).mockImplementation(async () => {
      for (const part of operation.remaining) {
        const entry = state.entries.find(e => e.entry_id === part.entry_id)!;
        expect(entry.count).toBeGreaterThanOrEqual(part.count);
        const ids = Array.from({ length: part.count }, () => { queuedIds.push(queuedIds.length + 1); return queuedIds.length; });
        operation.created.push({ entry_id: part.entry_id, count: part.count, queue_kind: 'auto', queue_item_ids: ids });
        entry.count -= part.count;
      }
      state = { ...state, revision: state.revision + 1, epoch: state.epoch + 1,
        entries: state.entries.filter(e => e.count > 0), active_operation: null };
      operation = { ...operation, remaining: [], status: 'complete' };
      return structuredClone(operation);
    });
    vi.mocked(api.getOrderPlan).mockResolvedValueOnce({ ...plan, inputs_token: 'inputs', mode: 'working', working_revision: 1 })
      .mockImplementation(() => outcome === 'failed' ? Promise.reject(new Error('context unavailable')) : new Promise(() => {}));
    render(<PlanBlock order={order} canEdit />);
    fireEvent.click(await screen.findByTestId('plan-enqueue-all'));
    await waitFor(() => expect(operation.status).toBe('complete'));
    await waitFor(() => expect(vi.mocked(api.getWorkingPlan).mock.calls.length).toBeGreaterThanOrEqual(3));
    // Exercise a second click if stale context incorrectly brought the button back.
    const second = screen.queryByTestId('plan-enqueue-all');
    if (second) await userEvent.setup().click(second);
    expect(queuedIds).toHaveLength(2);
    expect(state.entries).toEqual([]);
    expect(api.registerPlanSubmission).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.getWorkingPlan).mock.calls.length).toBeLessThanOrEqual(4);
  });

  it('submits a plan over LAN HTTP without crypto.randomUUID', async () => {
    const descriptor = Object.getOwnPropertyDescriptor(crypto, 'randomUUID');
    Object.defineProperty(crypto, 'randomUUID', { configurable: true, value: undefined });
    const random = vi.spyOn(crypto, 'getRandomValues');
    const send = vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [] });
    try {
      render(<PlanBlock order={order} canEdit />);
      fireEvent.click(await screen.findByTestId('plan-enqueue-all'));
      await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
      expect(random).toHaveBeenCalled();
      expect(vi.mocked(api.registerPlanSubmission).mock.calls[0][1].operation_id)
        .toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    } finally {
      cleanup();
      if (descriptor) Object.defineProperty(crypto, 'randomUUID', descriptor);
      else Reflect.deleteProperty(crypto, 'randomUUID');
    }
  });

  // The other two doors into the same submission, from PR #89 (@UVCXanth): a
  // row's own button and the order's review dialog both register and execute
  // one operation, and over plain LAN HTTP `crypto.randomUUID` is absent.
  it.each([
    ['one row', [{ line_id: 10, plate_id: 200, count: 1 }]],
    ['the order review', [{ line_id: 10, plate_id: 100, count: 1 }, { line_id: 10, plate_id: 200, count: 1 }]],
  ] as const)('submits %s over LAN HTTP without crypto.randomUUID', async (door, items) => {
    const descriptor = Object.getOwnPropertyDescriptor(crypto, 'randomUUID');
    Object.defineProperty(crypto, 'randomUUID', { configurable: true, value: undefined });
    const send = vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [] });
    try {
      if (door === 'one row') {
        render(<PlanBlock order={order} canEdit />);
        fireEvent.click(await screen.findByTestId('plan-row-10-200-queue'));
      } else {
        render(<PlanBlock order={order} canEdit queueRequest={{ requestedAt: 0 }} onQueueRequestClose={vi.fn()} />);
        const review = await screen.findByRole('dialog');
        const button = within(review).getByRole('button', { name: 'Send to auto queue' });
        await waitFor(() => expect(button).toBeEnabled());
        fireEvent.click(button);
      }
      await waitFor(() => expect(api.actPlanSubmission).toHaveBeenCalledTimes(1));

      expect(api.registerPlanSubmission).toHaveBeenCalledTimes(1);
      const [orderId, body] = vi.mocked(api.registerPlanSubmission).mock.calls[0];
      expect(orderId).toBe(order.id);
      expect(body.operation_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      // The parts registered are exactly the ones this door selected.
      expect(body.parts).toHaveLength(items.length);
      expect(api.actPlanSubmission).toHaveBeenCalledWith(order.id, body.operation_id, 'execute');
      // …and they are what reaches the queue, once.
      expect(send).toHaveBeenCalledTimes(1);
      expect(send).toHaveBeenCalledWith(order.id, { target: { kind: 'auto' }, items });
    } finally {
      cleanup();
      if (descriptor) Object.defineProperty(crypto, 'randomUUID', descriptor);
      else Reflect.deleteProperty(crypto, 'randomUUID');
    }
  });

  it('reconciles the payload after a lost execution response and continues the same operation', async () => {
    let committed = false;
    let complete = false;
    let state: import('../../../api/client').WorkingPlan = { schema_version: 1, revision: 0, entries: [],
      saved_revision: 0, saved_at: null, saved_by: null, epoch: 0, saved_epoch: null,
      active_operation: null, inputs_token: 'inputs', changed: false, issues: [] };
    const status = () => ({ operation_id: 'operation', status: complete ? 'complete' as const : 'pending' as const,
      created: committed ? [{ entry_id: 'row-10-100', count: 1, queue_kind: 'auto', queue_item_ids: [77] }] : [],
      remaining: complete ? [] : [{ entry_id: 'row-10-200', count: 1 }] });
    vi.spyOn(api, 'getWorkingPlan').mockImplementation(async () => structuredClone(state));
    vi.spyOn(api, 'writeWorkingPlan').mockImplementation(async (_, body) => {
      state = { ...state, revision: state.revision + 1, entries: body.entries }; return structuredClone(state);
    });
    vi.spyOn(api, 'registerPlanSubmission').mockImplementation(async (_, body) => {
      state.active_operation = body.operation_id; return { ...status(), operation_id: body.operation_id };
    });
    vi.spyOn(api, 'getPlanSubmission').mockImplementation(async () => status());
    const execute = vi.spyOn(api, 'actPlanSubmission').mockImplementation(async () => {
      if (!committed) {
        committed = true;
        state = { ...state, revision: state.revision + 1, epoch: 1, entries: state.entries.filter(e => e.plate_id === 200) };
        throw new Error('response lost after commit');
      }
      complete = true;
      state = { ...state, revision: state.revision + 1, active_operation: null, entries: [] };
      return status();
    });
    vi.spyOn(api, 'getOrderPlan').mockImplementation(async () => committed ? ({ ...plan, mode: 'working', working_revision: state.revision,
      lines: [{ ...plan.lines[0], rows: complete ? [] : [plan.lines[0].rows[1]] }] }) : plan);
    const legacy = vi.spyOn(api, 'enqueueOrderPlan');
    render(<PlanBlock order={order} canEdit />);
    fireEvent.click(await screen.findByTestId('plan-enqueue-all'));
    await waitFor(() => expect(screen.queryByTestId('plan-row-10-100')).toBeNull());
    expect(screen.getByTestId('plan-row-10-200-count')).toHaveValue(1);
    expect(screen.getByTestId('plan-row-10-200-count')).toBeDisabled();
    expect(screen.getByTestId('plan-row-10-200-inc')).toBeDisabled();
    expect(screen.getByTestId('plan-row-10-200-dec')).toBeDisabled();
    expect(api.writeWorkingPlan).toHaveBeenCalledTimes(1);
    const operation = vi.mocked(api.registerPlanSubmission).mock.calls[0][1].operation_id;
    fireEvent.click(screen.getByRole('button', { name: 'Continue submission' }));
    await waitFor(() => expect(execute).toHaveBeenCalledTimes(2));
    expect(execute.mock.calls[1][1]).toBe(operation);
    await waitFor(() => expect(screen.queryByTestId('plan-row-10-200')).toBeNull());
    expect(api.registerPlanSubmission).toHaveBeenCalledTimes(1);
    expect(api.writeWorkingPlan).toHaveBeenCalledTimes(1);
    expect(legacy).not.toHaveBeenCalled();
  });

  it('puts nearest quantity adjustment beside whole-plan enqueue without sending work', async () => {
    const open = vi.fn();
    const enqueue = vi.spyOn(api, 'enqueueOrderPlan');
    render(<PlanBlock order={order} canEdit onPlateFit={open} />);
    const button = await screen.findByRole('button', { name: 'Adjust without surplus' });
    expect(screen.getByTestId('plan-total')).toContainElement(button);
    fireEvent.click(button);
    expect(open).toHaveBeenCalledTimes(1);
    expect(enqueue).not.toHaveBeenCalled();
  });

  it.each(['page', 'dialog'] as const)('shows only the covered status without stale-baseline prompts in %s', async (variant) => {
    vi.mocked(api.getOrderPlan).mockResolvedValue({ ...plan, lines: [] });
    const state = await api.getWorkingPlan(order.id);
    vi.mocked(api.getWorkingPlan).mockResolvedValue({ ...state, revision: 4, epoch: 1,
      saved_epoch: 0, saved_revision: 2, saved_at: '2026-10-20T16:00:00Z', saved_by: 'operator',
      inputs_changed: true, changed: true, saved_machine_seconds: 64800 });
    const save = vi.spyOn(api, 'actWorkingPlan');
    const open = vi.fn();
    render(<PlanBlock order={order} canEdit variant={variant} onPlateFit={open} />);
    await screen.findByTestId('plan-empty');
    await waitFor(() => expect(screen.queryByTestId('working-plan-controls')).not.toBeInTheDocument());
    expect(screen.queryByTestId('plan-intro')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Adjust without surplus' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Print parameters for this order' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('order-forecast-panel')).not.toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    expect(api.writeWorkingPlan).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });

  it('keeps planning available for an empty draft with uncovered demand', async () => {
    vi.mocked(api.getOrderPlan).mockResolvedValue({ ...plan, lines: [{ ...plan.lines[0], rows: [] }] });
    render(<PlanBlock order={order} canEdit />);
    expect(await screen.findByTestId('working-plan-controls')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-empty')).not.toBeInTheDocument();
  });

  it('keeps the covered view during a background refetch, then shows newly needed work', async () => {
    const empty = { ...plan, lines: [] };
    vi.mocked(api.getOrderPlan).mockResolvedValue(empty);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    function Refresh() {
      const cache = useQueryClient();
      return <button onClick={() => void cache.invalidateQueries({ queryKey: ['project-plan', order.id] })}>refresh plan</button>;
    }
    render(<QueryClientProvider client={client}><Refresh /><PlanBlock order={order} canEdit /></QueryClientProvider>);
    await screen.findByTestId('plan-empty');
    await waitFor(() => expect(screen.queryByTestId('working-plan-controls')).not.toBeInTheDocument());
    let finish = (_value: typeof plan) => {};
    vi.mocked(api.getOrderPlan).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: 'refresh plan' }));
    await waitFor(() => expect(client.isFetching({ queryKey: ['project-plan', order.id] })).toBe(1));
    expect(screen.getByTestId('plan-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('working-plan-controls')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-intro')).not.toBeInTheDocument();
    finish(plan);
    expect(await screen.findByTestId('plan-row-10-100')).toBeInTheDocument();
    expect(screen.getByTestId('working-plan-controls')).toBeInTheDocument();
  });

  it('keeps receipt recovery visible even when the submitted draft has no rows', async () => {
    vi.mocked(api.getOrderPlan).mockResolvedValue({ ...plan, lines: [] });
    const state = await api.getWorkingPlan(order.id);
    vi.mocked(api.getWorkingPlan).mockResolvedValue({ ...state, revision: 1, active_operation: 'pending-empty' });
    render(<PlanBlock order={order} canEdit />);
    expect(await screen.findByRole('button', { name: 'Continue submission' })).toBeInTheDocument();
    expect(screen.getByTestId('working-plan-controls')).toBeInTheDocument();
  });

  it('applies exact run counts to the existing editor and keeps them on an identical refresh', async () => {
    render(<PlanBlock order={order} canEdit fitDraft={{ orderId: order.id, lineId: 10, quantity: 12, rows: [{ ...plan.lines[0].rows[0], count: 0 }, { ...plan.lines[0].rows[1], count: 2 }].filter(r => r.count > 0), revision: 1, planSignature: JSON.stringify(plan.lines[0]) }} />);
    await waitFor(() => expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(0));
    expect(screen.getByTestId('plan-row-10-200-count')).toHaveValue(2);
    expect(screen.getByTestId('plan-totals-prints')).toHaveTextContent('2');
  });

  it('preserves the fleet split across equal-yield model variants without adding duplicate rows', async () => {
    const base = { ...planWithAlternative, lines: [{ ...planWithAlternative.lines[0], rows: [
      { ...planWithAlternative.lines[0].rows[0], count: 10 },
    ] }] };
    vi.mocked(api.getOrderPlan).mockResolvedValue(base);
    const main = base.lines[0].rows[0];
    const enqueue = vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [] });
    render(<PlanBlock order={order} canEdit fitDraft={{ orderId: order.id, lineId: 10, quantity: 12,
      rows: [{ ...main, count: 6 }, { ...main, ...otherModel, count: 4, alternatives: [] }],
      revision: 1, planSignature: JSON.stringify(base.lines[0]) }} />);
    await waitFor(() => expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(10));
    expect(screen.queryByTestId('plan-row-10-400')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('plan-row-10-100-split'));
    expect(screen.getByTestId('plan-row-10-100-split-100')).toHaveValue(6);
    expect(screen.getByTestId('plan-row-10-100-split-400')).toHaveValue(4);
    fireEvent.click(screen.getByTestId('plan-enqueue-all'));
    await waitFor(() => expect(enqueue).toHaveBeenCalledWith(1, { items: [
      { line_id: 10, plate_id: 100, count: 6 }, { line_id: 10, plate_id: 400, count: 4 },
    ], target: { kind: 'auto' } }));
  });

  it('offers untouched equivalent files to the calculator while respecting an explicit file choice', async () => {
    vi.mocked(api.getOrderPlan).mockResolvedValue(planWithAlternative);
    const selection = vi.fn();
    render(<PlanBlock order={order} canEdit onPlateSelection={selection} />);
    await waitFor(() => expect(selection).toHaveBeenLastCalledWith({ 10: [100, 400, 200] }));
    fireEvent.change(screen.getByTestId('plan-row-10-100-file'), { target: { value: '400' } });
    await waitFor(() => expect(selection).toHaveBeenLastCalledWith({ 10: [400, 200] }));
    fireEvent.change(screen.getByTestId('plan-row-10-200-count'), { target: { value: '0' } });
    await waitFor(() => expect(selection).toHaveBeenLastCalledWith({ 10: [400] }));
  });

  it('renders the recommended plates of every line', async () => {
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100')).toHaveTextContent('big.3mf');
    expect(screen.getByTestId('plan-row-10-200')).toHaveTextContent('small.3mf');
    expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(1);
    // The whole file, not "plate 0".
    expect(screen.getByTestId('plan-row-10-200')).toHaveTextContent(/whole file/i);
    expect(screen.getByTestId('plan-totals-prints')).toHaveTextContent('2');
    // `0 && <jsx>` renders the NUMBER, and this block is full of counts that
    // legitimately reach zero — the detector is the only thing that sees one
    // left behind where a conditional used to be.
    expect(strayZeroTextNodes(screen.getByTestId('plan-block'))).toHaveLength(0);
  });

  it('opens with what the plan is, in one sentence', async () => {
    render(<PlanBlock order={order} canEdit />);
    expect(await screen.findByTestId('plan-intro')).toHaveTextContent(
      'What to print next to close the lines. What is printed, printing and queued is already subtracted.',
    );
  });

  it('heads a line with its configuration, what is left, the material and the machine time', async () => {
    const configured = {
      ...order,
      lines: [
        {
          ...order.lines[0],
          mode: 'product',
          configuration: {
            choices: [{ group_id: 1, group_name: 'Mount', option_id: 4, option_name: 'DIN', is_default: false }],
            changed_parts: [],
          },
        },
      ],
    } as unknown as Order;
    render(<PlanBlock order={configured} canEdit />);

    const head = await screen.findByTestId('plan-line-10-head');
    expect(head).toHaveTextContent('Flask');
    expect(head).toHaveTextContent('Mount: DIN');
    expect(head).toHaveTextContent('Outstanding: Body × 12');
    expect(head).toHaveTextContent('PETG');
    // 1 h + 30 min of the two rows at their planned counts.
    expect(screen.getByTestId('plan-line-10-machine')).toHaveTextContent('≈ 1h 30m of machine time');
    fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));
    expect(screen.getByTestId('plan-line-10-machine')).toHaveTextContent('≈ 2h 30m of machine time');
  });

  it('marks a count that left the plan with what the plan said, and a hand-added row with nothing', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithSpare);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([...plates, spare]);
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-count')).toHaveValue(1);
    expect(screen.queryByTestId('plan-row-10-100-planned')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));
    expect(screen.getByTestId('plan-row-10-100-planned')).toHaveTextContent('plan: 1');
    fireEvent.click(screen.getByTestId('plan-row-10-100-dec'));
    expect(screen.queryByTestId('plan-row-10-100-planned')).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId('plan-line-10-add'), { target: { value: '300' } });
    fireEvent.click(screen.getByTestId('plan-row-10-300-inc'));
    expect(screen.queryByTestId('plan-row-10-300-planned')).not.toBeInTheDocument();
  });

  it('names the plate and the model under the file', async () => {
    render(<PlanBlock order={order} canEdit />);
    const plate = await screen.findByTestId('plan-row-10-100-plate');
    expect(plate).toHaveTextContent('plate 1');
    expect(plate).toHaveTextContent('X1C');
    // No model, no chip — and never a «—» in its place.
    // Lower-case like «plate N» it stands for (final review M8).
    expect(screen.getByTestId('plan-row-10-200-plate')).toHaveTextContent(/^whole file$/);
  });

  it('adds the plan up in one line under the lines', async () => {
    render(<PlanBlock order={order} canEdit />);
    const total = await screen.findByTestId('plan-total');
    await waitFor(() => expect(total).toHaveTextContent('Total: 2 prints · 1h 30m · 120.0 g · ₴2.40 of filament'));
    expect(within(total).getByTestId('plan-enqueue-all')).toBeInTheDocument();
  });

  it('leaves an unknown filament cost out of the total line', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], rows: plan.lines[0].rows.map((row) => ({ ...row, cost: null })) }],
    });
    render(<PlanBlock order={order} canEdit />);
    const total = await screen.findByTestId('plan-total');
    expect(total).toHaveTextContent('Total: 2 prints · 1h 30m · 120.0 g');
    expect(total).not.toHaveTextContent('of filament');
  });

  it('says what is not sliced inside the no-plate block, beside the link to the product', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({ ...plan, lines: [{ ...plan.lines[0], not_sliced: [300] }] });
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([...plates, { ...spare, filename: 'raw.stl', sliced: false }]);
    render(<PlanBlock order={order} canEdit />);

    const block = await screen.findByTestId('plan-unsatisfiable-10-2');
    await waitFor(() => expect(block).toHaveTextContent('not sliced: raw.stl'));
    expect(block).toHaveTextContent('No plate for «Cap» × 3 in PETG.');
    expect(within(block).getByRole('link', { name: 'Link a file in the product' })).toHaveAttribute(
      'href',
      '/products/7#files',
    );
    expect(block.closest('table')).toBeNull();
  });

  it('says when the engine stopped early, and says nothing when it did not', async () => {
    // ⚠️ A truncated plan looks EXACTLY like a finished one — rows, totals, no
    // unsatisfiable part — so the only sign the operator can have is this line.
    // Printing everything on screen still leaves the order short.
    render(<PlanBlock order={order} canEdit />);
    expect(await screen.findByTestId('plan-row-10-100')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-truncated')).not.toBeInTheDocument();
    cleanup();

    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({ ...plan, truncated: true });
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-truncated')).toHaveTextContent(/stopped early/i);
  });

  it('leaves no bare zero behind when a row is emptied', async () => {
    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-200-dec'));

    // The count INPUT holds "0" as a value, not as a text node, so the row at
    // zero is still expected to leave the tree clean.
    expect(strayZeroTextNodes(screen.getByTestId('plan-block'))).toHaveLength(0);
  });

  it('recomputes the surplus and the totals from the plate yields when a count is raised', async () => {
    render(<PlanBlock order={order} canEdit />);

    // The projection wins the moment the recipes resolve: 10 + 6 bodies against
    // a need of 12 is 4, where the server said 3 and the rows' `useful` says 0.
    const surplus = await screen.findByTestId('plan-line-10-surplus');
    await waitFor(() => expect(surplus).toHaveTextContent('Body +4'));

    fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));

    expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(2);
    // 2 × 10 + 1 × 6 = 26 bodies, 14 more than the 12 outstanding.
    await waitFor(() => expect(screen.getByTestId('plan-line-10-surplus')).toHaveTextContent('Body +14'));
    expect(screen.getByTestId('plan-totals-prints')).toHaveTextContent('3');
    expect(screen.getByTestId('plan-totals-time')).toHaveTextContent('2h 30m');
    expect(screen.getByTestId('plan-totals-grams')).toHaveTextContent('220.0');
    expect(screen.getByTestId('plan-totals-cost')).toHaveTextContent('₴4.40');
  });

  it('sends only the rows left standing, and drops the caches the order is read from', async () => {
    const enqueue = vi
      .spyOn(api, 'enqueueOrderPlan')
      .mockResolvedValue({ created: [{ line_id: 10, plate_id: 100, queue_item_ids: [77] }] });
    const onProbeFetch = vi.fn();

    render(
      <>
        <PlanBlock order={order} canEdit />
        <OrderProbe id={1} onFetch={onProbeFetch} />
      </>,
    );

    fireEvent.click(await screen.findByTestId('plan-row-10-200-dec'));
    expect(screen.getByTestId('plan-row-10-200-count')).toHaveValue(0);

    fireEvent.click(screen.getByTestId('plan-enqueue-all'));

    await waitFor(() =>
      expect(enqueue).toHaveBeenCalledWith(1, {
        items: [{ plate_id: 100, count: 1, line_id: 10 }],
        target: { kind: 'auto' },
      }),
    );
    // The plan itself is re-read, and so is the order behind it.
    await waitFor(() => expect(vi.mocked(api.getOrderPlan).mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(vi.mocked(api.getOrderPlan).mock.calls.length).toBeLessThanOrEqual(3);
    await waitFor(() => expect(onProbeFetch.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(onProbeFetch.mock.calls.length).toBeLessThanOrEqual(3);
  });

  it('blocks both queue buttons while order parameters are unsaved, then sends the same plan', async () => {
    const options = {
      bed_levelling: 'on' as const, flow_cali: 'on' as const, nozzle_offset_cali: 'off' as const,
      layer_inspect: false, timelapse: false, timelapse_storage: null,
      mesh_mode_fast_check: true, gcode_injection: false,
      preheat_override: 'inherit' as const, preheat_chamber_target_override: null,
    };
    const context = { overrides: {}, models: [{
      model: 'A1 Mini', options, inherited: options, custom: false,
      capabilities: { known: true, supported: { bed_levelling: true, flow_cali: true, nozzle_offset_cali: false },
        auto: { bed_levelling: false, flow_cali: false, nozzle_offset_cali: false } },
    }] };
    vi.mocked(api.getOrderPrintOptions).mockResolvedValue(context);
    const save = vi.spyOn(api, 'setOrderPrintOptions').mockImplementation(async (_id, overrides) => ({ ...context, overrides }));
    const enqueue = vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [] });
    render(<PlanBlock order={order} canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: 'Print parameters for this order' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Use custom parameters for this order and model' }));
    await waitFor(() => expect(screen.getByTestId('plan-enqueue-all')).toBeDisabled());
    expect(screen.getByTestId('plan-row-10-100-queue')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save parameters' }));
    await waitFor(() => expect(screen.getByTestId('plan-enqueue-all')).not.toBeDisabled());
    expect(save).toHaveBeenCalledWith(1, { 'A1 Mini': options });
    fireEvent.click(screen.getByTestId('plan-row-10-100-queue'));
    await waitFor(() => expect(enqueue).toHaveBeenCalledWith(1, {
      items: [{ line_id: 10, plate_id: 100, count: plan.lines[0].rows[0].count }], target: { kind: 'auto' },
    }));
  });

  it('offers Rebalance on a line with pending router rows, calls the API and re-reads the order', async () => {
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'queue:create', 'queue:update_all']);
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], pending_auto_prints: 2 }],
    });
    const rebalance = vi
      .spyOn(api, 'rebalanceOrderLine')
      .mockResolvedValue({ converted: 1, created: 2, cancelled: 0, moved_parts: 6, skipped: [] });
    const onProbeFetch = vi.fn();

    render(
      <>
        <PlanBlock order={order} canEdit />
        <OrderProbe id={1} onFetch={onProbeFetch} />
      </>,
    );

    fireEvent.click(await screen.findByTestId('plan-line-10-rebalance'));

    await waitFor(() => expect(rebalance).toHaveBeenCalledWith(1, 10));
    await waitFor(() => expect(api.getOrderPlan).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onProbeFetch).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Moved 6 parts: 1 prints re-targeted, 2 added')).toBeInTheDocument();
  });

  it('says why the line did not move instead of claiming nothing would finish sooner', async () => {
    // `pending_auto_prints` counts rows the procedure itself refuses — pinned,
    // staged, scheduled — so the button shows for a line of pinned rows, and
    // "nothing would finish sooner" would then be a lie about the farm.
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'queue:create', 'queue:update_all']);
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], pending_auto_prints: 2 }],
    });
    vi.spyOn(api, 'rebalanceOrderLine').mockResolvedValue({
      converted: 0,
      created: 0,
      cancelled: 0,
      moved_parts: 0,
      skipped: [{ item_id: 1, reason: 'pinned' }],
    });

    render(<PlanBlock order={order} canEdit />);
    fireEvent.click(await screen.findByTestId('plan-line-10-rebalance'));

    expect(await screen.findByText('Pinned to specific filament slots — not moved')).toBeInTheDocument();
    expect(screen.queryByText('Nothing would finish sooner on another model')).toBeNull();
  });

  it('keeps the farm answer when nothing was refused and nothing moved', async () => {
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'queue:create', 'queue:update_all']);
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], pending_auto_prints: 2 }],
    });
    vi.spyOn(api, 'rebalanceOrderLine').mockResolvedValue({
      converted: 0,
      created: 0,
      cancelled: 0,
      moved_parts: 0,
      skipped: [],
    });

    render(<PlanBlock order={order} canEdit />);
    fireEvent.click(await screen.findByTestId('plan-line-10-rebalance'));

    expect(await screen.findByText('Nothing would finish sooner on another model')).toBeInTheDocument();
  });

  it('hides Rebalance when the line has nothing pending in the router, or the operator may not rewrite the queue', async () => {
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'queue:create', 'queue:update_all']);
    render(<PlanBlock order={order} canEdit />);
    await screen.findByTestId('plan-line-10');
    expect(screen.queryByTestId('plan-line-10-rebalance')).toBeNull();

    cleanup();
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'queue:create']);
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], pending_auto_prints: 2 }],
    });
    render(<PlanBlock order={order} canEdit />);
    await screen.findByTestId('plan-line-10');
    expect(screen.queryByTestId('plan-line-10-rebalance')).toBeNull();
  });

  it('names a part no plate can make, and points at the product’s files', async () => {
    render(<PlanBlock order={order} canEdit />);

    // ⚠️ The id carries the LINE beside the part: a part id is unique per
    // product, not per order, so two lines of one product would collide on a
    // bare `plan-unsatisfiable-2`.
    const row = await screen.findByTestId('plan-unsatisfiable-10-2');
    expect(row).toHaveTextContent(/no plate for/i);
    expect(row).toHaveTextContent('Cap');
    expect(row).toHaveTextContent('PETG');
    expect(within(row).getByRole('link')).toHaveAttribute('href', '/products/7#files');
    // The slice slot is reserved, not offered.
    expect(within(row).getByRole('button')).toBeDisabled();
  });

  it('plans nothing for a closed order', async () => {
    render(<PlanBlock order={{ ...order, status: 'completed' }} canEdit />);

    const closed = await screen.findByTestId('plan-closed');
    expect(closed).toHaveTextContent('The order is closed');
    expect(closed).toHaveTextContent('No plan is built for it.');
    expect(screen.queryByTestId('plan-row-10-100')).not.toBeInTheDocument();
    expect(api.getOrderPlan).not.toHaveBeenCalled();
  });

  it('says so when there is nothing outstanding left to plan', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      lines: [{ ...plan.lines[0], rows: [], outstanding_before: [], unsatisfiable: [], surplus_after: [] }],
      totals: { rows: 0, prints: 0, print_time_seconds: 0, filament_used_grams: 0, cost: null },
    });

    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-empty')).toHaveTextContent(
      'Everything is printed or already queued — nothing to print.',
    );
    expect(screen.queryByTestId('plan-enqueue-all')).not.toBeInTheDocument();
  });

  it('keeps the block and offers a retry when the plan cannot be loaded', async () => {
    // ⚠️ A block that returns null on a failed fetch is simply absent from the
    // page, which reads as "this order has nothing to print" — the one thing a
    // failed plan must not say.
    const get = vi.spyOn(api, 'getOrderPlan').mockRejectedValue(new Error('Gateway timeout'));

    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-error')).toHaveTextContent(/could not load the plan/i);
    // The block stays on the page (its own heading went to the «Print plan» tab, WS-13 E3 F05).
    expect(screen.getByTestId('plan-block')).toBeInTheDocument();

    get.mockResolvedValue(plan);
    fireEvent.click(screen.getByTestId('plan-retry'));

    expect(await screen.findByTestId('plan-row-10-100')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-error')).not.toBeInTheDocument();
  });

  it('says nothing it cannot know when the plan names a line the order has lost', async () => {
    // ⚠️ Without the line there is no way to tell which parts it counts, so the
    // plate yields are UNKNOWN — not unrestricted. An unrestricted yield would
    // report another product's parts on a shared plate as this line's surplus.
    // The server's own `surplus_after` is what is shown instead.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], line_id: 99, not_sliced: [300, 400] }],
    });
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([
      ...plates,
      { ...plates[1], id: 300, filename: 'raw.stl', sliced: false },
    ]);

    render(<PlanBlock order={order} canEdit />);

    // The recipes have resolved — this text is drawn from them.
    expect(await screen.findByText(/raw\.stl/)).toBeInTheDocument();
    // Still the server's 3, not the recipes' 4.
    expect(screen.getByTestId('plan-line-99-surplus')).toHaveTextContent('Body +3');
    // A plate id the recipes never named is left out, not printed as `#400`.
    expect(screen.getByTestId('plan-line-99')).not.toHaveTextContent('#400');
  });

  it('gates the queue and the printer on their own permissions', async () => {
    // ⚠️ Two different answers. Filing work under a queue is `queue:create`;
    // opening PrintModal dispatches to a machine and is `printers:control`.
    // Somebody trusted with the paperwork is not thereby trusted to start a
    // print, and vice versa.
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'printers:control']);
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-printer')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-queue')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-enqueue-all')).not.toBeInTheDocument();

    cleanup();
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'queue:create']);
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-queue')).toBeInTheDocument();
    expect(screen.getByTestId('plan-enqueue-all')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-printer')).not.toBeInTheDocument();
  });

  // WS-13 E13 O21 (final review #4): every door of the plan files new work under the order —
  // Fф, `orders:update` or `orders:file_prints` — beside the queue's or the printer's own right.
  // A printer operator who only reads orders sends nothing the server would refuse.
  it('offers a reader of orders neither the queue nor the printer, however much it may print', async () => {
    auth.granted = new Set(['orders:read', 'products:read', 'queue:create', 'printers:control']);
    render(<PlanBlock order={order} canEdit={false} />);

    expect(await screen.findByTestId('plan-row-10-100')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-enqueue-all')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-queue')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-printer')).not.toBeInTheDocument();
  });

  it('offers a filing clerk the queue and the printer without the right to change orders', async () => {
    auth.granted = new Set(['orders:read', 'orders:file_prints', 'products:read', 'queue:create', 'printers:control']);
    // The order page passes Fф as the plan's `canEdit` (OrderView).
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-queue')).toBeInTheDocument();
    expect(screen.getByTestId('plan-enqueue-all')).toBeInTheDocument();
    expect(screen.getByTestId('plan-row-10-100-printer')).toBeInTheDocument();
  });

  it('names a plate from a file the caller cannot open, and offers no printer for it', async () => {
    // WS-13 E1 CL2: the server keeps the plate and drops its file's name.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithSpare);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([...plates, { ...spare, filename: null, hidden: true }]);
    render(<PlanBlock order={order} canEdit />);

    const add = await screen.findByTestId('plan-line-10-add');
    expect(within(add).getByRole('option', { name: /File you cannot open/ })).toBeInTheDocument();
    fireEvent.change(add, { target: { value: '300' } });

    const row = screen.getByTestId('plan-row-10-300');
    expect(within(row).getByText(/File you cannot open/)).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-300-printer')).not.toBeInTheDocument();
    // The planned rows keep their printer.
    expect(screen.getByTestId('plan-row-10-100-printer')).toBeInTheDocument();
  });

  // ---- a file the reader may not open, named by the server (WS-13 E1 CL6) ----

  /** The row's own X1C file and its alternative (P1S unless told), each hidden or not. */
  const planHiding = (rowHidden: boolean, altHidden: boolean, altModel = 'P1S'): OrderPlan => ({
    ...planWithAlternative,
    lines: [
      {
        ...planWithAlternative.lines[0],
        rows: [
          {
            ...plan.lines[0].rows[0],
            filename: rowHidden ? null : 'big.3mf',
            hidden: rowHidden,
            alternatives: [
              { ...otherModel, printer_model: altModel, filename: altHidden ? null : 'big-p1s.3mf', hidden: altHidden },
            ],
          },
          plan.lines[0].rows[1],
        ],
      },
    ],
  });

  const pickOption = (pick: HTMLElement, value: string) =>
    pick.querySelector(`option[value="${value}"]`) as HTMLOptionElement;

  /** «To printer…» on a row with alternatives opens F09 (WS-13 E4 E09): its printer
   *  select is where the row's old inline picker was. */
  const openPrinterDialog = async (row: string) => {
    fireEvent.click(await screen.findByTestId(`plan-row-${row}-printer`));
    const dialog = await screen.findByRole('dialog', { name: 'To printer' });
    return within(dialog).findByLabelText('Printer');
  };
  /** Choose a printer and press «Next» — the step the inline picker's change used to be. */
  const choosePrinter = (pick: HTMLElement, value: string) => {
    fireEvent.change(pick, { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: 'Next — print dialog' }));
  };

  it('labels a hidden server row and offers no printer for it', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      ...plan,
      lines: [
        {
          ...plan.lines[0],
          rows: [{ ...plan.lines[0].rows[0], filename: null, hidden: true }, plan.lines[0].rows[1]],
        },
      ],
    });
    render(<PlanBlock order={order} canEdit />);

    const row = await screen.findByTestId('plan-row-10-100');
    expect(within(row).getByText('File you cannot open')).toBeInTheDocument();
    expect(row).not.toHaveTextContent('null');
    expect(screen.queryByTestId('plan-row-10-100-printer')).not.toBeInTheDocument();
    // Queueing is not a file action: the row still goes to the queue by its plate.
    expect(screen.getByTestId('plan-row-10-100-queue')).toBeInTheDocument();
    expect(screen.getByTestId('plan-row-10-200-printer')).toBeInTheDocument();
  });

  it('labels a hidden alternative in the file switch and in the split', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planHiding(false, true));
    render(<PlanBlock order={order} canEdit />);

    const files = await screen.findByTestId('plan-row-10-100-file');
    expect([...files.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'big.3mf (X1C)',
      'File you cannot open (P1S)',
    ]);
    fireEvent.click(screen.getByTestId('plan-row-10-100-split'));
    const panel = screen.getByTestId('plan-row-10-100-split-panel');
    expect(panel).toHaveTextContent(/File you cannot open\s*P1S/);
    expect(panel).not.toHaveTextContent('null');
  });

  it('sends a printer only a plate the reader may open: hidden row, visible alternative', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planHiding(true, false));
    render(<PlanBlock order={order} canEdit />);

    const pick = await openPrinterDialog('10-100');
    // The X1C would get the row's own file — hidden; the P1S gets the alternative.
    expect(pickOption(pick, '1')).toBeDisabled();
    expect(pickOption(pick, '1')).toHaveTextContent('File you cannot open');
    expect(pickOption(pick, '2')).not.toBeDisabled();

    choosePrinter(pick, '1');
    expect(printModal.props).toBeNull();

    choosePrinter(pick, '2');
    expect(printModal.props).toMatchObject({ libraryFileId: 8, preselectedPlateId: 2, initialSelectedPrinterIds: [2] });
  });

  it('sends a printer only a plate the reader may open: visible row, hidden alternative', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planHiding(false, true));
    render(<PlanBlock order={order} canEdit />);

    const pick = await openPrinterDialog('10-100');
    expect(pickOption(pick, '2')).toBeDisabled();

    choosePrinter(pick, '2');
    expect(printModal.props).toBeNull();

    choosePrinter(pick, '1');
    expect(printModal.props).toMatchObject({ libraryFileId: 5, preselectedPlateId: 1, initialSelectedPrinterIds: [1] });
  });

  it('follows the file the operator chose: a visible alternative opens where the hidden row would not', async () => {
    // Two X1C exports: the printer cannot tell them apart, so the row's CHOICE
    // decides the plate — and the check is on that final plate.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planHiding(true, false, 'X1C'));
    render(<PlanBlock order={order} canEdit />);

    const pick = await openPrinterDialog('10-100');
    expect(pickOption(pick, '1')).toBeDisabled();
    choosePrinter(pick, '1');
    expect(printModal.props).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    fireEvent.change(screen.getByTestId('plan-row-10-100-file'), { target: { value: '400' } });
    const again = await openPrinterDialog('10-100');
    expect(pickOption(again, '1')).not.toBeDisabled();
    choosePrinter(again, '1');
    expect(printModal.props).toMatchObject({ libraryFileId: 8, initialSelectedPrinterIds: [1] });
  });

  it('offers no printer when every file of the row is hidden', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planHiding(true, true));
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-file')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-printer')).not.toBeInTheDocument();
  });

  // ---- the operator's edits vs. the farm's own events ----

  it('keeps the counts and the hand-added rows across a refetch that changed nothing', async () => {
    // ⚠️ `project-plan` is invalidated by every `print_complete` /
    // `archive_created` on the farm, because those events carry no
    // `project_id`. Reseeding on the CLOCK therefore wiped the operator's
    // half-made plan whenever any printer anywhere finished a job.
    const get = vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithSpare);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([...plates, spare]);

    render(
      <>
        <PlanBlock order={order} canEdit />
        <Refetcher id={1} />
      </>,
    );

    fireEvent.change(await screen.findByTestId('plan-line-10-add'), { target: { value: '300' } });
    fireEvent.change(screen.getByTestId('plan-row-10-100-count'), { target: { value: '5' } });
    expect(screen.getByTestId('plan-row-10-300')).toBeInTheDocument();

    // The product's NAME is the one thing that moves, and it is deliberately
    // outside the signature: it is what makes the arrival of the second
    // response observable at all (an identical payload changes nothing on
    // screen, so nothing can be waited for), and it is itself a change the
    // counts must survive.
    get.mockResolvedValue({
      ...planWithSpare,
      lines: [{ ...planWithSpare.lines[0], product_name: 'Flask, renamed' }],
    });
    fireEvent.click(screen.getByTestId('force-refetch'));

    expect(await screen.findByText('Flask, renamed')).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(5);
    expect(screen.getByTestId('plan-row-10-300')).toBeInTheDocument();
  });

  it('keeps manual counts when current need changes', async () => {
    const get = vi.spyOn(api, 'getOrderPlan').mockResolvedValue(plan);

    render(
      <>
        <PlanBlock order={order} canEdit />
        <Refetcher id={1} />
      </>,
    );

    fireEvent.change(await screen.findByTestId('plan-row-10-100-count'), { target: { value: '5' } });
    expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(5);

    // The new need changes coverage, not the operator's selected counts.
    get.mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], outstanding_before: [{ part_id: 1, name: 'Body', count: 6 }] }],
    });
    fireEvent.click(screen.getByTestId('force-refetch'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(5));
  });

  it('keeps one copy and the manual count of an added plate when recommendations change', async () => {
    const get = vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithSpare);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([...plates, spare]);

    render(
      <>
        <PlanBlock order={order} canEdit />
        <Refetcher id={1} />
      </>,
    );

    fireEvent.change(await screen.findByTestId('plan-line-10-add'), { target: { value: '300' } });
    expect(screen.getAllByTestId('plan-row-10-300')).toHaveLength(1);

    get.mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], candidates: [100, 200, 300], rows: [...plan.lines[0].rows, spareRow] }],
    });
    fireEvent.click(screen.getByTestId('force-refetch'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(screen.getByTestId('plan-row-10-300-count')).toHaveValue(1));
    expect(screen.getAllByTestId('plan-row-10-300')).toHaveLength(1);
  });

  // ---- the affordances themselves ----

  it('refuses to queue more than 999 of one plate, and says why', async () => {
    render(<PlanBlock order={order} canEdit />);

    fireEvent.change(await screen.findByTestId('plan-row-10-100-count'), { target: { value: '1000' } });

    // ⚠️ Disabled with the reason, never clamped: 1000 is a number the
    // operator typed on purpose, and silently turning it into 999 would queue
    // work nobody asked for.
    expect(screen.getByTestId('plan-row-10-100-queue')).toBeDisabled();
    expect(screen.getByTestId('plan-row-10-100-queue')).toHaveAttribute('title', 'At most 999 per plate');
    expect(screen.getByTestId('plan-enqueue-all')).toBeDisabled();
    expect(screen.getByTestId('plan-enqueue-all')).toHaveAttribute('title', 'At most 999 per plate');

    fireEvent.change(screen.getByTestId('plan-row-10-100-count'), { target: { value: '999' } });

    expect(screen.getByTestId('plan-row-10-100-queue')).toBeEnabled();
    expect(screen.getByTestId('plan-enqueue-all')).toBeEnabled();
  });

  it('sends one row on its own button, and only that row', async () => {
    const enqueue = vi
      .spyOn(api, 'enqueueOrderPlan')
      .mockResolvedValue({ created: [{ line_id: 10, plate_id: 200, queue_item_ids: [7] }] });

    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-200-inc'));
    fireEvent.click(screen.getByTestId('plan-row-10-200-queue'));

    await waitFor(() =>
      expect(enqueue).toHaveBeenCalledWith(1, {
        items: [{ plate_id: 200, count: 2, line_id: 10 }],
        target: { kind: 'auto' },
      }),
    );
    expect(enqueue).toHaveBeenCalledTimes(1);
  });

  it('prices a hand-added plate at the rate the plan itself implies', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithSpare);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([...plates, spare]);

    render(<PlanBlock order={order} canEdit />);

    fireEvent.change(await screen.findByTestId('plan-line-10-add'), { target: { value: '300' } });

    const row = screen.getByTestId('plan-row-10-300');
    expect(row).toHaveTextContent('extra.3mf');
    // One print, and the figures come from the RECIPE — the plan never
    // mentioned this plate.
    expect(screen.getByTestId('plan-row-10-300-count')).toHaveValue(1);
    expect(row).toHaveTextContent('15m');
    expect(row).toHaveTextContent('50.0');
    // ⚠️ No cost on the wire for a plate nobody planned: 2.00 for 100 g on
    // row 100 is the farm's rate, and 50 g of it is 1.00.
    expect(row).toHaveTextContent('₴1.00');
  });

  it('offers the add-plate menu to somebody who can only print', async () => {
    // Adding a row is a client-side what-if — nothing is written anywhere. A
    // `printers:control` user reaches the printer button through it.
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'printers:control']);
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithSpare);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([...plates, spare]);

    render(<PlanBlock order={order} canEdit />);

    fireEvent.change(await screen.findByTestId('plan-line-10-add'), { target: { value: '300' } });

    expect(screen.getByTestId('plan-row-10-300-printer')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-300-queue')).not.toBeInTheDocument();
  });

  it('sends nothing, and says so, when every row is at zero', async () => {
    const enqueue = vi.spyOn(api, 'enqueueOrderPlan');

    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-100-dec'));
    fireEvent.click(screen.getByTestId('plan-row-10-200-dec'));
    fireEvent.click(screen.getByTestId('plan-enqueue-all'));

    expect(await screen.findByText('Nothing to send — every row is at zero.')).toBeInTheDocument();
    expect(enqueue).not.toHaveBeenCalled();
  });

  it('names the steppers and says why a button at zero is disabled', async () => {
    render(<PlanBlock order={order} canEdit />);

    const row = within(await screen.findByTestId('plan-row-10-100'));
    expect(row.getByLabelText('Fewer')).toBeInTheDocument();
    expect(row.getByLabelText('More')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('plan-row-10-100-dec'));

    expect(screen.getByTestId('plan-row-10-100-dec')).toBeDisabled();
    expect(screen.getByTestId('plan-row-10-100-dec')).toHaveAttribute('title', 'Nothing to send at 0');
    expect(screen.getByTestId('plan-row-10-100-queue')).toBeDisabled();
    expect(screen.getByTestId('plan-row-10-100-queue')).toHaveAttribute('title', 'Nothing to send at 0');
  });

  it('heads the plate table with five named columns, the actions one for a screen reader', async () => {
    render(<PlanBlock order={order} canEdit />);

    const headers = within(await screen.findByTestId('plan-line-10')).getAllByRole('columnheader');
    expect(headers.map((h) => h.textContent)).toEqual(['Plate', 'Covers', 'Prints', 'Time / filament', 'Actions']);
    expect(within(headers[4]).getByText('Actions')).toHaveClass('sr-only');
  });

  it('puts a row’s filament cost under its time and grams, where the cost column was', async () => {
    render(<PlanBlock order={order} canEdit />);

    const figures = await screen.findByTestId('plan-row-10-100-figures');
    await waitFor(() => expect(figures).toHaveTextContent('₴2.00'));
    expect(figures).toHaveTextContent('1h');
    expect(figures).toHaveTextContent('100.0 g');
  });

  it('hands the printer leg the file, the plate, the order and the line', async () => {
    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-100-printer'));

    expect(printModal.props).toMatchObject({
      libraryFileId: 5,
      preselectedPlateId: 1,
      projectId: 1,
      projectLineId: 10,
      // Routing, not dispatching: "to printer…" already answered the only
      // question the toggle asks, so the modal opens on that leg and stays.
      initialDispatchMode: 'specific',
      lockDispatchMode: true,
    });

    cleanup();
    printModal.props = null;
    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-200-printer'));

    // ⚠️ `plate_index` 0 is "the whole file", not "plate 0" — the modal must
    // be handed nothing rather than a zero it would pin.
    // The cast restores what the re-render actually put back: `props` was set
    // to `null` four lines up, and flow analysis cannot see the modal's own
    // assignment inside the mock.
    expect((printModal.props as Record<string, unknown> | null)?.preselectedPlateId).toBeUndefined();
  });
  it('marks the two QUEUE caches stale beside every order view, once a plate is queued', async () => {
    // ⚠️ `['queue']` and `['auto-queue']` are NOT order views and cannot join
    // `ORDER_VIEW_KEYS`: no order page reads either, and enqueueing is the only
    // order action that puts rows in a printer's queue and in the auto-queue
    // distributor. Drop them and the queue page keeps showing the old count
    // for its whole `staleTime` after a plan is sent.
    vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({
      created: [{ line_id: 10, plate_id: 200, queue_item_ids: [7] }],
    });
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries');

    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-200-queue'));

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['queue'] }),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['auto-queue'] });
    for (const key of ORDER_VIEW_KEYS) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: [key] });
    }
  });

  // ---- the block as a landmark ----

  it('marks itself on every branch and keeps an anchor to link to', async () => {
    // ⚠️ `plan-block` answers "is the plan on this page", not "has the plan
    // arrived" — a marker carried only by the loaded branch turns "the page
    // mounts the block" into a question that can be asked one tick early and
    // answered no. `order-plan` is the id a link into the plan needs; the old
    // anchor went with the printer picker and nothing replaced it.
    const branch = async () => {
      const section = await screen.findByTestId('plan-block');
      expect(section).toHaveAttribute('id', 'order-plan');
    };

    render(<PlanBlock order={{ ...order, status: 'completed' }} canEdit />);
    expect(await screen.findByTestId('plan-closed')).toBeInTheDocument();
    await branch();
    cleanup();

    // A plan that never answers: the loading branch, held open.
    vi.spyOn(api, 'getOrderPlan').mockReturnValue(new Promise(() => {}));
    render(<PlanBlock order={order} canEdit />);
    await branch();
    cleanup();

    vi.spyOn(api, 'getOrderPlan').mockRejectedValue(new Error('Gateway timeout'));
    render(<PlanBlock order={order} canEdit />);
    expect(await screen.findByTestId('plan-error')).toBeInTheDocument();
    await branch();
    cleanup();

    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(plan);
    render(<PlanBlock order={order} canEdit />);
    expect(await screen.findByTestId('plan-row-10-100')).toBeInTheDocument();
    await branch();
  });

  it('keeps the block on the FIFTH branch too, when the query is pending but not fetching', async () => {
    // ⚠️ `isLoading` is `isPending && isFetching`. A query that is pending and
    // NOT fetching — paused because the browser is offline, which is also what a
    // backgrounded tab and a dropped network look like — falls through the
    // closed, loading and error branches with no data at all. Returning `null`
    // there took the whole section off the page, which reads as "this order has
    // nothing to print": the same lie the error branch exists to avoid, from a
    // state that is not even a failure.
    onlineManager.setOnline(false);
    try {
      vi.spyOn(api, 'getOrderPlan').mockResolvedValue(plan);
      render(<PlanBlock order={order} canEdit />);

      const section = await screen.findByTestId('plan-block');
      expect(section).toHaveAttribute('id', 'order-plan');
      expect(within(section).getByTestId('plan-idle')).toHaveTextContent(/loading/i);
      expect(screen.queryByTestId('plan-row-10-100')).not.toBeInTheDocument();
    } finally {
      onlineManager.setOnline(true);
    }
  });

  // ---- a plate with no estimate ----

  it('shows no time for a hand-added plate whose file gives no estimate, and voids the total', async () => {
    // ⚠️ The wire says `null`, never `0`: `list_plates` normalises through the
    // same `estimate_seconds` the plan engine ranks with, so "0 seconds" and
    // "no estimate" cannot mean two things on one screen. `rowFromRecipe` maps
    // that null to `time_unknown`, and one unknown row voids the footer's time
    // rather than letting a partial sum read as a promise.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithSpare);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([
      ...plates,
      { ...spare, print_time_seconds: null },
    ]);

    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-totals-time')).toHaveTextContent('1h 30m');

    fireEvent.change(await screen.findByTestId('plan-line-10-add'), { target: { value: '300' } });

    const row = screen.getByTestId('plan-row-10-300');
    expect(within(row).getAllByText('—').length).toBeGreaterThan(0);
    expect(row).not.toHaveTextContent(/\b0s\b/);
    expect(screen.getByTestId('plan-totals-time')).toHaveTextContent('—');
    // The grams the plate DOES carry are still summed: one missing figure voids
    // its own column, never the row.
    expect(screen.getByTestId('plan-totals-grams')).toHaveTextContent('170.0');
  });

  // ---- the reseed is per line ----

  it('preserves manual decisions on both lines when one need changes', async () => {
    // ⚠️ The signature is per LINE because the `added` reconciliation beside it
    // always was. A whole-plan signature meant an edited count on one line
    // reverted whenever ANOTHER line moved — and `project-plan` is invalidated
    // by every print event on the farm, so "another line moved" is an ordinary
    // event nobody on this page caused.
    const get = vi.spyOn(api, 'getOrderPlan').mockResolvedValue(twoLinePlan);

    render(
      <>
        <PlanBlock order={twoLineOrder} canEdit />
        <Refetcher id={1} />
      </>,
    );

    fireEvent.change(await screen.findByTestId('plan-row-10-100-count'), { target: { value: '5' } });
    fireEvent.change(screen.getByTestId('plan-row-20-100-count'), { target: { value: '7' } });

    // Only line 10's outstanding moves. Line 20's plan comes back byte for byte.
    get.mockResolvedValue({
      ...twoLinePlan,
      lines: [
        { ...twoLinePlan.lines[0], outstanding_before: [{ part_id: 1, name: 'Body', count: 6 }] },
        twoLinePlan.lines[1],
      ],
    });
    fireEvent.click(screen.getByTestId('force-refetch'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(5));
    expect(screen.getByTestId('plan-row-20-100-count')).toHaveValue(7);
  });

  // ---- the same part, sliced once per printer model (pass 7, decision 6) ----

  it('offers the other file the part is sliced for, and follows it in every figure', async () => {
    // ⚠️ The engine's greedy picks ONE plate and hangs every print on it, so
    // the second file was invisible in the block even though the operator owns
    // both machines. This switch is the whole fix.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    const enqueue = vi
      .spyOn(api, 'enqueueOrderPlan')
      .mockResolvedValue({ created: [{ line_id: 10, plate_id: 400, queue_item_ids: [7] }] });

    render(<PlanBlock order={order} canEdit />);

    const files = await screen.findByTestId('plan-row-10-100-file');
    expect(files).toHaveAccessibleName('File');
    expect([...files.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'big.3mf (X1C)',
      'big-p1s.3mf (P1S)',
    ]);
    // The row with no alternative keeps its plain filename, and no switch.
    expect(screen.queryByTestId('plan-row-10-200-file')).not.toBeInTheDocument();
    expect(screen.getByTestId('plan-totals-time')).toHaveTextContent('1h 30m');

    fireEvent.change(files, { target: { value: '400' } });

    const row = screen.getByTestId('plan-row-10-100');
    expect(row).toHaveTextContent('150.0');
    expect(row).toHaveTextContent('₴3.00');
    // 7200 + 1800 seconds, 150 + 20 grams, 3.00 + 0.40 — the count never moves,
    // because the two files make the same parts.
    expect(screen.getByTestId('plan-totals-prints')).toHaveTextContent('2');
    expect(screen.getByTestId('plan-totals-time')).toHaveTextContent('2h 30m');
    expect(screen.getByTestId('plan-totals-grams')).toHaveTextContent('170.0');
    expect(screen.getByTestId('plan-totals-cost')).toHaveTextContent('₴3.40');

    fireEvent.click(screen.getByTestId('plan-row-10-100-queue'));

    await waitFor(() =>
      expect(enqueue).toHaveBeenCalledWith(1, {
        items: [{ plate_id: 400, count: 1, line_id: 10 }],
        target: { kind: 'auto' },
      }),
    );
  });

  it('ends a long file name in the switch with an ellipsis, not at the border (WS-13 E4 F6)', async () => {
    // This field alone opts out of the customizable select, which ignores
    // `text-overflow`: a file name rarely fits a table cell. The price — the
    // system's own list for this one field — is the owner's call.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);

    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-file')).toHaveClass('select-ellipsis');
  });

  it('keeps a row’s actions on one line on a phone — on the order page, not in the dialog (WS-13 E4 F6)', async () => {
    const { unmount } = render(<PlanBlock order={order} canEdit />);
    const onPage = (await screen.findByTestId('plan-row-10-100-queue')).parentElement!;
    expect(onPage).toHaveClass('flex-wrap', 'max-sm:flex-nowrap');
    unmount();

    render(<PlanBlock order={order} canEdit variant="dialog" />);
    const inDialog = (await screen.findByTestId('plan-row-10-100-queue')).parentElement!;
    expect(inDialog).toHaveClass('flex-wrap');
    expect(inDialog).not.toHaveClass('max-sm:flex-nowrap');
  });

  it('hands the printer leg the file that printer was sliced for', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);

    render(<PlanBlock order={order} canEdit />);

    choosePrinter(await openPrinterDialog('10-100'), '2');

    expect(printModal.props).toMatchObject({
      // The P1S file, not the row's own — and the printer the operator named
      // travels with it, pinned rather than hidden.
      libraryFileId: 8,
      preselectedPlateId: 2,
      initialSelectedPrinterIds: [2],
      lockPrinterSelection: true,
      projectId: 1,
      projectLineId: 10,
      initialDispatchMode: 'specific',
    });

    cleanup();
    printModal.props = null;
    render(<PlanBlock order={order} canEdit />);

    choosePrinter(await openPrinterDialog('10-100'), '1');

    expect(printModal.props).toMatchObject({ libraryFileId: 5, preselectedPlateId: 1, initialSelectedPrinterIds: [1] });
  });

  it('matches an alternative to a printer whose model row is the long marketing name', async () => {
    // ⚠️ The two sides of this comparison are spelled by different writers: a
    // `Printer.model` column carries whatever the machine (or the operator)
    // called itself, and the plate's `printer_model` is the 3MF's, normalised to
    // the short name. Compared with `mapModelCode` alone — which passes a long
    // name straight through — nothing ever matched, and the menu silently kept
    // the row's own file for every printer named the long way.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    vi.spyOn(api, 'getPrinters').mockResolvedValue([
      { id: 3, name: 'Longhand', model: 'Bambu Lab P1S', is_active: true },
    ] as unknown as Printer[]);

    render(<PlanBlock order={order} canEdit />);

    choosePrinter(await openPrinterDialog('10-100'), '3');

    // The P1S alternative, not the row's own X1C file.
    expect(printModal.props).toMatchObject({ libraryFileId: 8, preselectedPlateId: 2 });
  });

  it('does not offer a parked printer', async () => {
    // ⚠️ Maintenance Mode is an axis of its own: the card stays on the printers
    // page and the machine takes no work. (Archived printers never arrive —
    // `getPrinters` leaves them out.)
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    vi.spyOn(api, 'getPrinters').mockResolvedValue([
      { id: 1, name: 'Carbon', model: 'X1C', is_active: true },
      { id: 2, name: 'Parked', model: 'P1S', is_active: false },
    ] as unknown as Printer[]);

    render(<PlanBlock order={order} canEdit />);

    const pick = await openPrinterDialog('10-100');
    await waitFor(() => expect(pick.querySelectorAll('option')).toHaveLength(1));
    expect([...pick.querySelectorAll('option')].map((o) => o.textContent)).toEqual(['Carbon (X1C)']);
  });

  it('offers only printers of the models the row was sliced for', async () => {
    // ⚠️ The row stands for an X1C file and a P1S file — those two models are
    // the whole set of machines that can close it. Both sides go through the same
    // normaliser, so a printer named the long way still qualifies.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    vi.spyOn(api, 'getPrinters').mockResolvedValue([
      { id: 1, name: 'Carbon', model: 'X1C', is_active: true },
      { id: 2, name: 'Pea', model: 'P1S', is_active: true },
      { id: 4, name: 'Mini', model: 'A1 Mini', is_active: true },
      { id: 5, name: 'Longhand', model: 'Bambu Lab X1 Carbon', is_active: true },
    ] as unknown as Printer[]);

    render(<PlanBlock order={order} canEdit />);

    const pick = await openPrinterDialog('10-100');
    await waitFor(() => expect(pick.querySelectorAll('option')).toHaveLength(3));
    expect([...pick.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'Carbon (X1C)',
      'Pea (P1S)',
      'Longhand (Bambu Lab X1 Carbon)',
    ]);
  });

  it("filters no printer when the row's files carry no model", async () => {
    // An old 3MF without the metadata says nothing about its machine. When
    // nothing is known, every active printer beats none.
    const modelless: OrderPlan = {
      ...planWithAlternative,
      lines: [
        {
          ...planWithAlternative.lines[0],
          rows: [
            {
              ...planWithAlternative.lines[0].rows[0],
              printer_model: null,
              alternatives: [{ ...otherModel, printer_model: null }],
            },
            planWithAlternative.lines[0].rows[1],
          ],
        },
      ],
    };
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(modelless);
    vi.spyOn(api, 'getPrinters').mockResolvedValue([
      { id: 1, name: 'Carbon', model: 'X1C', is_active: true },
      { id: 4, name: 'Mini', model: 'A1 Mini', is_active: true },
    ] as unknown as Printer[]);

    render(<PlanBlock order={order} canEdit />);

    const pick = await openPrinterDialog('10-100');
    await waitFor(() => expect(pick.querySelectorAll('option')).toHaveLength(2));
    expect([...pick.querySelectorAll('option')].map((o) => o.textContent)).toEqual(['Carbon (X1C)', 'Mini (A1 Mini)']);
  });

  it('says so when no active printer of those models exists, instead of offering the rest', async () => {
    // The only X2D is in Maintenance Mode and the P1S is archived: the honest
    // answer is a dialog that says why — not every mini on the farm.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    vi.spyOn(api, 'getPrinters').mockResolvedValue([
      { id: 4, name: 'Mini', model: 'A1 Mini', is_active: true },
      { id: 2, name: 'Parked', model: 'P1S', is_active: false },
    ] as unknown as Printer[]);

    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-100-printer'));
    const dialog = await screen.findByRole('dialog', { name: 'To printer' });
    expect(await within(dialog).findByText('No active printer of this model')).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Printer')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Next — print dialog' })).toBeDisabled();
  });

  it('says it is still reading the printers, and never calls a farm it has not read empty', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    vi.spyOn(api, 'getPrinters').mockReturnValue(new Promise(() => {}));

    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-100-printer'));
    const dialog = await screen.findByRole('dialog', { name: 'To printer' });
    expect(within(dialog).getByText('Loading printers…')).toBeInTheDocument();
    expect(within(dialog).queryByText('No active printer of this model')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Next — print dialog' })).toBeDisabled();
  });

  describe('the model matrix is read too (final review I2, R09)', () => {
    // The farm's only machine is an X1E, which takes the row's X1C file only through the matrix.
    const x1e = [{ id: 7, name: 'Echo', model: 'X1E', is_active: true }] as unknown as Printer[];

    it('says it is still reading while the matrix is, and never calls the farm empty', async () => {
      vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
      vi.spyOn(api, 'getPrinters').mockResolvedValue(x1e);
      vi.spyOn(api, 'getModelCompatibility').mockReturnValue(new Promise(() => {}));

      render(<PlanBlock order={order} canEdit />);

      fireEvent.click(await screen.findByTestId('plan-row-10-100-printer'));
      const dialog = await screen.findByRole('dialog', { name: 'To printer' });
      await waitFor(() => expect(api.getPrinters).toHaveBeenCalled());
      expect(await within(dialog).findByText('Loading printers…')).toBeInTheDocument();
      expect(within(dialog).queryByText('No active printer of this model')).not.toBeInTheDocument();
      expect(within(dialog).getByRole('button', { name: 'Next — print dialog' })).toBeDisabled();
    });

    it('says the matrix could not be read, offers to try again, and then offers the compatible printer', async () => {
      vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
      vi.spyOn(api, 'getPrinters').mockResolvedValue(x1e);
      const matrix = vi.spyOn(api, 'getModelCompatibility').mockRejectedValue(new Error('Gateway timeout'));

      render(<PlanBlock order={order} canEdit />);

      fireEvent.click(await screen.findByTestId('plan-row-10-100-printer'));
      const dialog = await screen.findByRole('dialog', { name: 'To printer' });
      expect(await within(dialog).findByText('Could not read which printer models fit these files')).toBeInTheDocument();
      expect(within(dialog).queryByText('No active printer of this model')).not.toBeInTheDocument();
      expect(within(dialog).getByRole('button', { name: 'Next — print dialog' })).toBeDisabled();

      matrix.mockResolvedValue({ models: { X1E: ['X1C'] } });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Try again' }));
      const pick = await within(dialog).findByLabelText('Printer');
      expect([...pick.querySelectorAll('option')].map((o) => o.textContent)).toEqual(['Echo (X1E)']);
    });
  });

  it('says the printers could not be read and offers to try again, instead of «no printer»', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    const get = vi.spyOn(api, 'getPrinters').mockRejectedValue(new Error('Gateway timeout'));

    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-100-printer'));
    const dialog = await screen.findByRole('dialog', { name: 'To printer' });
    expect(await within(dialog).findByText('Could not read the printers')).toBeInTheDocument();
    expect(within(dialog).queryByText('No active printer of this model')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Next — print dialog' })).toBeDisabled();

    get.mockResolvedValue(farm);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Try again' }));
    const pick = await within(dialog).findByLabelText('Printer');
    choosePrinter(pick, '2');
    expect(printModal.props).toMatchObject({ libraryFileId: 8, initialSelectedPrinterIds: [2] });
  });

  it('checks the choice again before going on: a printer that left the farm opens nothing', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    const get = vi.spyOn(api, 'getPrinters').mockResolvedValue(farm);

    render(
      <>
        <PlanBlock order={order} canEdit />
        <PrintersRefetcher />
      </>,
    );

    const pick = await openPrinterDialog('10-100');
    fireEvent.change(pick, { target: { value: '2' } });
    get.mockResolvedValue([farm[0]]);
    fireEvent.click(screen.getByTestId('force-printers-refetch'));

    expect(await screen.findByText('This printer is no longer available — choose another.')).toBeInTheDocument();
    const next = screen.getByRole('button', { name: 'Next — print dialog' });
    expect(next).toBeDisabled();
    fireEvent.click(next);
    expect(printModal.props).toBeNull();
  });

  it('checks the file again before going on: a file that became hidden opens nothing', async () => {
    const get = vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planHiding(false, false));

    render(
      <>
        <PlanBlock order={order} canEdit />
        <Refetcher id={1} />
      </>,
    );

    const pick = await openPrinterDialog('10-100');
    fireEvent.change(pick, { target: { value: '2' } });
    get.mockResolvedValue(planHiding(false, true));
    fireEvent.click(screen.getByTestId('force-refetch'));

    expect(
      await screen.findByText('This printer would get a file you cannot open — choose another.'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next — print dialog' }));
    expect(printModal.props).toBeNull();
  });

  it('names the file, the plate and the models in the dialog', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-100-printer'));
    const dialog = await screen.findByRole('dialog', { name: 'To printer' });
    expect(dialog).toHaveTextContent('big.3mf · plate 1');
    expect(dialog).toHaveTextContent('(X1C / P1S)');
  });

  it('splits a row across the two files, and refuses a split that does not add up', async () => {
    // ⚠️ The auto-queue routes an item by its `target_model`, so a file only
    // ever reaches the printers it was sliced for — splitting the count is the
    // only way one line's work reaches two models at once.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    const enqueue = vi
      .spyOn(api, 'enqueueOrderPlan')
      .mockResolvedValue({ created: [{ line_id: 10, plate_id: 100, queue_item_ids: [7] }] });

    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-100-split'));

    // Everything on the file the row is showing, which is what "no split" means.
    expect(screen.getByTestId('plan-row-10-100-split-100')).toHaveValue(1);
    expect(screen.getByTestId('plan-row-10-100-split-400')).toHaveValue(0);
    expect(screen.queryByTestId('plan-row-10-100-split-error')).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId('plan-row-10-100-split-400'), { target: { value: '2' } });

    expect(screen.getByTestId('plan-row-10-100-split-error')).toHaveTextContent('Counts must add up to 1');
    expect(screen.getByTestId('plan-row-10-100-split-apply')).toBeDisabled();
    expect(screen.getByTestId('plan-row-10-100-queue')).toBeDisabled();
    expect(screen.getByTestId('plan-enqueue-all')).toBeDisabled();
    // ⚠️ And it SAYS why. A whole-plan button greyed out by an edit made three
    // rows down is otherwise a dead end — the row's own error is on screen, but
    // nothing connects the two.
    expect(screen.getByTestId('plan-enqueue-all')).toHaveAttribute(
      'title',
      'A split does not add up to its row',
    );

    fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));
    fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));

    expect(screen.queryByTestId('plan-row-10-100-split-error')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('plan-row-10-100-split-apply'));

    await waitFor(() =>
      expect(enqueue).toHaveBeenCalledWith(1, {
        items: [
          { plate_id: 100, count: 1, line_id: 10 },
          { plate_id: 400, count: 2, line_id: 10 },
        ],
        target: { kind: 'auto' },
      }),
    );
  });

  it('carries one item per file into the whole-plan body', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    const enqueue = vi
      .spyOn(api, 'enqueueOrderPlan')
      .mockResolvedValue({ created: [{ line_id: 10, plate_id: 100, queue_item_ids: [7] }] });

    render(<PlanBlock order={order} canEdit />);

    fireEvent.click(await screen.findByTestId('plan-row-10-100-split'));
    fireEvent.change(screen.getByTestId('plan-row-10-100-split-400'), { target: { value: '2' } });
    fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));
    fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));

    fireEvent.click(screen.getByTestId('plan-enqueue-all'));

    await waitFor(() =>
      expect(enqueue).toHaveBeenCalledWith(1, {
        items: [
          { plate_id: 100, count: 1, line_id: 10 },
          { plate_id: 400, count: 2, line_id: 10 },
          { plate_id: 200, count: 1, line_id: 10 },
        ],
        target: { kind: 'auto' },
      }),
    );
  });

  it('shows the farm proposal in the split panel and applies it only on click', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    vi.spyOn(api, 'getOrderForecast').mockResolvedValue(proposing);
    render(<PlanBlock order={order} canEdit />);
    expect(await screen.findByTestId('plan-line-10-ready')).toHaveTextContent(/ready ≈/);
    // WS-13 E4 E07/E08: the proposal lives in the panel now, not in the actions cell —
    // under each file's own field, because a list labelled by model reads nothing when
    // two files are for the same model.
    expect(screen.queryByTestId('plan-row-10-100-proposal-100')).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('plan-row-10-100-split'));
    expect(await screen.findByTestId('plan-row-10-100-proposal-100')).toHaveTextContent('by the farm: 0');
    expect(screen.getByTestId('plan-row-10-100-proposal-400')).toHaveTextContent('by the farm: 1');
    expect(screen.queryByTestId('plan-row-10-100-proposal')).not.toBeInTheDocument();
    // Opening the editor shows the default — every print on the row's own file — not the proposal.
    expect(screen.getByTestId('plan-row-10-100-split-100')).toHaveValue(1);
    expect(screen.getByTestId('plan-row-10-100-split-400')).toHaveValue(0);
    await userEvent.click(screen.getByTestId('plan-row-10-100-apply-farm'));
    expect(screen.getByTestId('plan-row-10-100-split-100')).toHaveValue(0);
    expect(screen.getByTestId('plan-row-10-100-split-400')).toHaveValue(1);
    expect(screen.queryByTestId('plan-forecast-stale')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-line-10-ready')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-proposal-100')).not.toBeInTheDocument();

    // The comparison is against effective enqueue items, not whether an editor
    // map exists: returning exactly to the server plan makes its forecast fresh.
    fireEvent.change(screen.getByTestId('plan-row-10-100-split-100'), { target: { value: '1' } });
    fireEvent.change(screen.getByTestId('plan-row-10-100-split-400'), { target: { value: '0' } });
    expect(screen.queryByTestId('plan-forecast-stale')).not.toBeInTheDocument();
    expect(screen.getByTestId('plan-line-10-ready')).toBeInTheDocument();
    expect(screen.getByTestId('plan-row-10-100-proposal-400')).toHaveTextContent('by the farm: 1');
  });

  it('opens the split as a row of its own under the plate, not inside the actions cell', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    render(<PlanBlock order={order} canEdit />);

    const toggle = await screen.findByTestId('plan-row-10-100-split');
    expect(toggle).toHaveTextContent('Split across files');
    fireEvent.click(toggle);
    const row = screen.getByTestId('plan-row-10-100');
    const panelRow = screen.getByTestId('plan-row-10-100-split-panel').closest('tr') as HTMLElement;
    expect(panelRow).not.toBe(row);
    expect(row.nextElementSibling).toBe(panelRow);
    expect(within(panelRow).getByRole('cell')).toHaveAttribute('colspan', '5');
    expect(toggle).toHaveTextContent('Hide split');
    fireEvent.click(toggle);
    expect(screen.queryByTestId('plan-row-10-100-split-panel')).not.toBeInTheDocument();
  });

  it('lets a reader open the farm proposal and read it, with nothing to change or send', async () => {
    // R06: without `queue:create` the panel is read-only — the numbers, and no field,
    // no «apply», no «queue split».
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust']);
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    vi.spyOn(api, 'getOrderForecast').mockResolvedValue(proposing);
    render(<PlanBlock order={order} canEdit />);

    const toggle = await screen.findByTestId('plan-row-10-100-split');
    await waitFor(() => expect(toggle).toHaveTextContent('Farm proposal'));
    fireEvent.click(toggle);
    const panel = screen.getByTestId('plan-row-10-100-split-panel');
    // The numbers stand beside their files; no second list repeats them by model.
    expect(within(panel).queryByTestId('plan-row-10-100-proposal')).not.toBeInTheDocument();
    expect(panel).toHaveTextContent(/big\.3mf\s*X1C\s*— 0/);
    expect(panel).toHaveTextContent(/big-p1s\.3mf\s*P1S\s*— 1/);
    expect(within(panel).queryByRole('spinbutton')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-apply-farm')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-split-apply')).not.toBeInTheDocument();
    expect(toggle).toHaveTextContent('Hide proposal');

    // A draft hides the proposal here too: the panel closes with its toggle.
    fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));
    await waitFor(() => expect(screen.queryByTestId('plan-row-10-100-split')).not.toBeInTheDocument());
    expect(screen.queryByTestId('plan-row-10-100-split-panel')).not.toBeInTheDocument();
  });

  it('gives a reader no split toggle when the farm proposes nothing', async () => {
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust']);
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-file')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-split')).not.toBeInTheDocument();
  });

  it('uses the effective split for totals instead of the file shown before splitting', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-totals-time')).toHaveTextContent('1h 30m');
    expect(screen.getByTestId('plan-totals-grams')).toHaveTextContent('120.0');
    expect(screen.getByTestId('plan-totals-cost')).toHaveTextContent('₴2.40');

    await userEvent.click(screen.getByTestId('plan-row-10-100-split'));
    fireEvent.change(screen.getByTestId('plan-row-10-100-split-100'), { target: { value: '0' } });
    fireEvent.change(screen.getByTestId('plan-row-10-100-split-400'), { target: { value: '1' } });

    // 2 h on the P1S file + the 30 min small plate; 150 g / $3 replace
    // 100 g / $2. Both totals must match the same entries enqueue would send.
    expect(screen.getByTestId('plan-totals-time')).toHaveTextContent('2h 30m');
    expect(screen.getByTestId('plan-totals-grams')).toHaveTextContent('170.0');
    expect(screen.getByTestId('plan-totals-cost')).toHaveTextContent('₴3.40');
  });

  it('keeps a row’s alternative out of the add-plate menu', async () => {
    // ⚠️ Adding it would put the same work on screen twice: the row already
    // offers that file as a switch, and a second row for it would be counted
    // again by every total.
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      ...planWithAlternative,
      lines: [{ ...planWithAlternative.lines[0], candidates: [100, 200, 300, 400] }],
    });
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([...plates, spare, alternativePlate]);

    render(<PlanBlock order={order} canEdit />);

    const add = await screen.findByTestId('plan-line-10-add');
    expect([...add.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'Add a plate…',
      'extra.3mf · plate 2',
    ]);
  });

  it('preserves the selected file until an explicit source review', async () => {
    // A source change is reviewed, never silently replaced by another file.
    const get = vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
    const withoutAlternatives: OrderPlan = {
      ...planWithAlternative,
      lines: [
        {
          ...planWithAlternative.lines[0],
          rows: [{ ...planWithAlternative.lines[0].rows[0], alternatives: [] }, planWithAlternative.lines[0].rows[1]],
        },
      ],
    };

    render(
      <>
        <PlanBlock order={order} canEdit />
        <Refetcher id={1} />
      </>,
    );

    fireEvent.change(await screen.findByTestId('plan-row-10-100-file'), { target: { value: '400' } });
    expect(screen.getByTestId('plan-row-10-100-file')).toHaveValue('400');
    // A count edit beside the switch: it is what MAKES the reseed observable
    // here. `chosenPlate` falls back to the row's own plate when the choice is
    // no longer offered, so a surviving choice against an empty alternatives
    // list renders identically to a dropped one — the three maps reseed
    // together, and this is the one of them that shows.
    fireEvent.change(screen.getByTestId('plan-row-10-100-count'), { target: { value: '3' } });
    expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(3);

    get.mockResolvedValue(withoutAlternatives);
    fireEvent.click(screen.getByTestId('force-refetch'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(screen.getByTestId('plan-row-10-100-file')).toHaveValue('400'));
    // The explicit file and count survive; revalidation is the server's decision.
    expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(3);
    expect(screen.getByTestId('plan-totals-time')).toHaveTextContent('6h 30m');
  });

  it('carries no heading of its own in the dialog variant either', () => {
    render(<PlanBlock order={order} canEdit variant="dialog" />);
    expect(screen.queryByRole('heading', { name: /what to print next/i })).not.toBeInTheDocument();
    expect(screen.getByTestId('plan-block')).toBeInTheDocument();
  });

  it('reports a successful enqueue to the caller', async () => {
    const onEnqueued = vi.fn();
    vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [{ line_id: 10, plate_id: 100, queue_item_ids: [77] }] });
    render(<PlanBlock order={order} canEdit variant="dialog" onEnqueued={onEnqueued} />);
    await userEvent.click(await screen.findByTestId('plan-enqueue-all'));
    await waitFor(() => expect(onEnqueued).toHaveBeenCalledTimes(1));
  });

  // ---- filament needs (spec 2026-09-07) ----
  // The block is the plan DIALOG's alone since WS-13 E3 (F06, R04): on the order
  // page the side panel shows the same body. These run in the dialog variant.

  it('leaves the filament to the side panel on the order page', async () => {
    const need = vi.spyOn(api, 'getOrderFilament').mockResolvedValue(EMPTY_NEEDS);
    render(<PlanBlock order={order} canEdit />);
    await screen.findByTestId('plan-block');
    expect(screen.queryByTestId('filament-needs')).toBeNull();
    expect(need).not.toHaveBeenCalled();
  });


  it('shows the filament the plan needs against the shelf, with the shortage in amber', async () => {
    vi.spyOn(api, 'getOrderFilament').mockResolvedValue({
      ...EMPTY_NEEDS,
      unknown_prints: 1,
      rows: [
        { material: 'PETG', colour: 'black', need_g: 3200, have_g: 1100, have_type_g: 4200, short_g: 2100, unknown_prints: 0 },
        { material: 'PLA', colour: null, need_g: 400, have_g: 900, have_type_g: 900, short_g: 0, unknown_prints: 1 },
      ],
    });
    render(<PlanBlock order={order} canEdit variant="dialog" />);
    const petg = await screen.findByTestId('filament-need-PETG-black');
    expect(petg).toHaveTextContent('PETG black');
    expect(petg).toHaveTextContent('3.2kg');
    expect(petg).toHaveTextContent('1.1kg');
    expect(petg).toHaveTextContent('4.2kg of PETG in total');
    expect(petg).toHaveTextContent('short 2.1kg');
    expect(petg).toHaveAttribute('data-short', 'true');
    const pla = screen.getByTestId('filament-need-PLA');
    expect(pla).toHaveTextContent('PLA');
    expect(pla).not.toHaveTextContent('in total');
    expect(pla).toHaveAttribute('data-short', 'false');
    expect(screen.getByText('1 print with unknown filament')).toBeInTheDocument();
    expect(screen.getByLabelText(/slicer estimate/)).toBeInTheDocument();
  });

  it('still shows the filament table when the plan has nothing outstanding', async () => {
    // ⚠️ The need is the plan's remainder PLUS the pending queue rows, so an
    // empty plan is precisely when the whole figure lives in the queue — and
    // the table used to sit inside the «nothing outstanding» else-branch,
    // hiding exactly then (final review I2).
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({ ...plan, lines: [] });
    vi.spyOn(api, 'getOrderFilament').mockResolvedValue({
      ...EMPTY_NEEDS,
      rows: [{ material: 'PETG', colour: null, need_g: 500, have_g: 900, have_type_g: 900, short_g: 0, unknown_prints: 0 }],
    });
    render(<PlanBlock order={order} canEdit variant="dialog" />);
    expect(await screen.findByTestId('plan-empty')).toBeInTheDocument();
    expect(await screen.findByTestId('filament-need-PETG')).toHaveTextContent('500g');
  });

  it('does not present a channel without a slicer weight as zero grams', async () => {
    vi.spyOn(api, 'getOrderFilament').mockResolvedValue({
      ...EMPTY_NEEDS,
      rows: [{ material: 'PETG', colour: null, need_g: 0, have_g: 900, have_type_g: 900, short_g: 0, unknown_prints: 1 }],
    });
    render(<PlanBlock order={order} canEdit variant="dialog" />);
    const row = await screen.findByTestId('filament-need-PETG');
    expect(row).toHaveTextContent('weight unknown');
    expect(row).toHaveTextContent('1 print without grams');
    expect(row).not.toHaveTextContent('need 0g');
  });

  it('says so when the shelf could not be read', async () => {
    vi.spyOn(api, 'getOrderFilament').mockResolvedValue({
      ...EMPTY_NEEDS, stock_unavailable: true,
      rows: [{ material: 'PETG', colour: null, need_g: 500, have_g: null, have_type_g: null, short_g: null, unknown_prints: 0 }],
    });
    render(<PlanBlock order={order} canEdit variant="dialog" />);
    const row = await screen.findByTestId('filament-need-PETG');
    expect(row).toHaveTextContent('500g');
    expect(screen.getByTitle('The shelf could not be read')).toBeInTheDocument();
  });
  describe('order and line auto-queue review', () => {
    const request = { requestedAt: 0 };
    const duplicateOrder = {
      ...order, name: 'Product A/B synthetic order',
      lines: [order.lines[0], { ...order.lines[0], id: 11 }],
    };
    const duplicatePlan = {
      ...plan, lines: [plan.lines[0], { ...plan.lines[0], line_id: 11 }],
    };

    it('reviews the whole order, then sends both duplicate-product lines exactly once', async () => {
      vi.spyOn(api, 'getOrderPlan').mockResolvedValue(duplicatePlan);
      const send = vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [] });
      const close = vi.fn();
      render(<PlanBlock order={duplicateOrder} canEdit queueRequest={request} onQueueRequestClose={close} />);
      const confirm = await screen.findByRole('dialog');
      await waitFor(() => expect(within(confirm).getByRole('button', { name: 'Send to auto queue' })).toBeEnabled());
      expect(confirm).toHaveTextContent('Total: 4 print runs');
      expect(confirm).toHaveTextContent('Line 1');
      expect(confirm).toHaveTextContent('Line 2');
      expect(confirm).toHaveTextContent('does not cover every outstanding part');
      expect(send).not.toHaveBeenCalled();
      const button = within(confirm).getByRole('button', { name: 'Send to auto queue' });
      fireEvent.click(button);
      fireEvent.click(button);
      await waitFor(() => expect(close).toHaveBeenCalledTimes(1));
      expect(send).toHaveBeenCalledTimes(1);
      expect(send).toHaveBeenCalledWith(order.id, { target: { kind: 'auto' }, items: [
        { line_id: 10, plate_id: 100, count: 1 }, { line_id: 10, plate_id: 200, count: 1 },
        { line_id: 11, plate_id: 100, count: 1 }, { line_id: 11, plate_id: 200, count: 1 },
      ] });
    });

    it('scopes by order-line id, preserving edited counts without sending its identical sibling', async () => {
      vi.spyOn(api, 'getOrderPlan').mockResolvedValue(duplicatePlan);
      const send = vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [] });
      const close = vi.fn();
      function Review() {
        const [open, setOpen] = useState(false);
        return <><button onClick={() => setOpen(true)}>review synthetic line</button>
          <PlanBlock order={duplicateOrder} canEdit queueRequest={open ? { ...request, lineId: 11 } : null}
            onQueueRequestClose={close} /></>;
      }
      render(<Review />);
      const input = await screen.findByTestId('plan-row-11-100-count');
      fireEvent.change(input, { target: { value: '3' } });
      fireEvent.change(screen.getByTestId('plan-row-11-200-count'), { target: { value: '0' } });
      fireEvent.click(screen.getByRole('button', { name: 'review synthetic line' }));
      const confirm = await screen.findByRole('dialog');
      expect(confirm).toHaveTextContent('Total: 3 print runs');
      expect(confirm).not.toHaveTextContent('Line 1');
      fireEvent.click(within(confirm).getByRole('button', { name: 'Send to auto queue' }));
      await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
      expect(send).toHaveBeenCalledWith(order.id, { target: { kind: 'auto' }, items: [{ line_id: 11, plate_id: 100, count: 3 }] });
    });

    it('cancel leaves the queue untouched', async () => {
      const send = vi.spyOn(api, 'enqueueOrderPlan');
      const close = vi.fn();
      render(<PlanBlock order={order} canEdit queueRequest={request} onQueueRequestClose={close} />);
      fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }));
      expect(close).toHaveBeenCalledOnce();
      expect(send).not.toHaveBeenCalled();
    });

    it('does not send an old cached plan or a pending refresh', async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
      client.setQueryData(['project-plan', order.id], plan, { updatedAt: 100 });
      let resolve!: (value: OrderPlan) => void;
      vi.spyOn(api, 'getOrderPlan').mockReturnValue(new Promise<OrderPlan>((done) => { resolve = done; }));
      const send = vi.spyOn(api, 'enqueueOrderPlan');
      render(<QueryClientProvider client={client}><PlanBlock order={order} canEdit queueRequest={{ requestedAt: 101 }} onQueueRequestClose={vi.fn()} /></QueryClientProvider>);
      const button = within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send to auto queue' });
      expect(button).toBeDisabled();
      fireEvent.click(button);
      expect(send).not.toHaveBeenCalled();
      resolve(plan);
      await waitFor(() => expect(button).toBeEnabled());
    });

    it('refuses a failed refresh even with cached data, and allows retry', async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
      client.setQueryData(['project-plan', order.id], plan);
      const read = vi.spyOn(api, 'getOrderPlan').mockRejectedValue(new Error('offline'));
      render(<QueryClientProvider client={client}><PlanBlock order={order} canEdit queueRequest={request} onQueueRequestClose={vi.fn()} /></QueryClientProvider>);
      const confirm = await screen.findByRole('dialog');
      const retry = await within(confirm).findByRole('button', { name: 'Retry' });
      expect(within(confirm).getByRole('button', { name: 'Send to auto queue' })).toBeDisabled();
      read.mockResolvedValue(plan);
      fireEvent.click(retry);
      await waitFor(() => expect(within(confirm).getByRole('button', { name: 'Send to auto queue' })).toBeEnabled());
    });

    it.each(['covered', 'closed', 'permission', 'over-cap'] as const)('refuses a %s plan', async (reason) => {
      if (reason === 'covered') vi.spyOn(api, 'getOrderPlan').mockResolvedValue({ ...plan, lines: [] });
      if (reason === 'permission') auth.granted.delete('queue:create');
      if (reason === 'over-cap') vi.spyOn(api, 'getOrderPlan').mockResolvedValue({ ...plan, lines: [{
        ...plan.lines[0], rows: [{ ...plan.lines[0].rows[0], count: 1000 }],
      }] });
      const send = vi.spyOn(api, 'enqueueOrderPlan');
      render(<PlanBlock order={reason === 'closed' ? { ...order, status: 'completed' } : order} canEdit queueRequest={request} onQueueRequestClose={vi.fn()} />);
      const confirm = await screen.findByRole('dialog');
      if (reason !== 'closed') await screen.findByTestId('plan-intro');
      const button = within(confirm).getByRole('button', { name: 'Send to auto queue' });
      expect(button).toBeDisabled();
      fireEvent.click(button);
      expect(send).not.toHaveBeenCalled();
    });

    it('keeps a server refusal in the confirmation and permits a deliberate retry', async () => {
      const send = vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [] });
      vi.mocked(api.registerPlanSubmission).mockRejectedValueOnce(new ApiError('Order was closed elsewhere', 409));
      const close = vi.fn();
      render(<PlanBlock order={order} canEdit queueRequest={request} onQueueRequestClose={close} />);
      const confirm = await screen.findByRole('dialog');
      const button = within(confirm).getByRole('button', { name: 'Send to auto queue' });
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      expect(await within(confirm).findByRole('alert')).toHaveTextContent('Order was closed elsewhere');
      expect(close).not.toHaveBeenCalled();
      fireEvent.click(button);
      await waitFor(() => expect(close).toHaveBeenCalledOnce());
      expect(api.registerPlanSubmission).toHaveBeenCalledTimes(2);
      expect(send).toHaveBeenCalledTimes(1);
    });
    it('does not retry an uncertain or partially committed send in the same review', async () => {
      const send = vi.spyOn(api, 'enqueueOrderPlan').mockRejectedValue(new ApiError('Writer failed after one item', 500));
      const close = vi.fn();
      render(<PlanBlock order={order} canEdit queueRequest={request} onQueueRequestClose={close} />);
      const confirm = await screen.findByRole('dialog');
      const button = within(confirm).getByRole('button', { name: 'Send to auto queue' });
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      expect((await within(confirm).findAllByRole('alert')).some(alert => alert.textContent?.includes('Read the submission status'))).toBe(true);
      expect(confirm).toHaveTextContent('Some tasks may already have been created');
      expect(button).toBeDisabled();
      fireEvent.click(button);
      expect(send).toHaveBeenCalledTimes(1);
      expect(close).not.toHaveBeenCalled();
    });

    it('reviews the existing model split and refuses its unfinished draft', async () => {
      vi.spyOn(api, 'getOrderPlan').mockResolvedValue(planWithAlternative);
      const send = vi.spyOn(api, 'enqueueOrderPlan').mockResolvedValue({ created: [] });
      function Review() {
        const [open, setOpen] = useState(false);
        return <><button onClick={() => setOpen(true)}>review split</button>
          <PlanBlock order={order} canEdit queueRequest={open ? request : null} onQueueRequestClose={() => setOpen(false)} /></>;
      }
      render(<Review />);
      fireEvent.click(await screen.findByTestId('plan-row-10-100-split'));
      fireEvent.change(screen.getByTestId('plan-row-10-100-split-400'), { target: { value: '2' } });
      fireEvent.click(screen.getByRole('button', { name: 'review split' }));
      let confirm = await screen.findByRole('dialog');
      expect(within(confirm).getByRole('button', { name: 'Send to auto queue' })).toBeDisabled();
      expect(confirm).toHaveTextContent('unfinished split');
      fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
      fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));
      fireEvent.click(screen.getByTestId('plan-row-10-100-inc'));
      fireEvent.click(screen.getByRole('button', { name: 'review split' }));
      confirm = await screen.findByRole('dialog');
      expect(confirm).toHaveTextContent('Total: 4 print runs');
      fireEvent.click(within(confirm).getByRole('button', { name: 'Send to auto queue' }));
      await waitFor(() => expect(send).toHaveBeenCalledOnce());
      expect(send).toHaveBeenCalledWith(order.id, { target: { kind: 'auto' }, items: [
        { line_id: 10, plate_id: 100, count: 1 }, { line_id: 10, plate_id: 400, count: 2 },
        { line_id: 10, plate_id: 200, count: 1 },
      ] });
    });

    it('blocks new shortcuts while model-specific print options have unsaved edits', async () => {
      vi.spyOn(api, 'getOrderPrintOptions').mockResolvedValue({ overrides: {}, models: [{
        model: 'P1S', custom: false, capabilities: { known: true,
          supported: { bed_levelling: true, flow_cali: false, nozzle_offset_cali: false, layer_inspect: false },
          auto: { bed_levelling: false, flow_cali: false, nozzle_offset_cali: false } },
        inherited: { ...DEFAULT_PRINT_OPTIONS }, options: { ...DEFAULT_PRINT_OPTIONS },
      }] });
      const send = vi.spyOn(api, 'enqueueOrderPlan');
      function Review() {
        const [open, setOpen] = useState(false);
        return <><button onClick={() => setOpen(true)}>review options</button>
          <PlanBlock order={order} canEdit queueRequest={open ? request : null} onQueueRequestClose={() => setOpen(false)} /></>;
      }
      render(<Review />);
      fireEvent.click(await screen.findByRole('button', { name: 'Print parameters for this order' }));
      fireEvent.click(await screen.findByRole('checkbox', { name: 'Use custom parameters for this order and model' }));
      fireEvent.click(screen.getByRole('button', { name: 'review options' }));
      const confirm = await screen.findByRole('dialog');
      expect(within(confirm).getByRole('button', { name: 'Send to auto queue' })).toBeDisabled();
      expect(within(confirm).getByRole('alert')).toHaveTextContent(/save|unsaved/i);
      expect(send).not.toHaveBeenCalled();
    });

  });

});
