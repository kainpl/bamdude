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
import { screen, fireEvent, waitFor, within, cleanup } from '@testing-library/react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import { api } from '../../../api/client';
import type { Order, OrderPlan, PlanRow as PlanRowData, Permission, PlateRecipe } from '../../../api/client';
import { PlanBlock } from '../../../components/projects/PlanBlock';

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
          count: 1,
          useful: [{ part_id: 1, name: 'Body', count: 10 }],
          print_time_seconds: 3600,
          filament_used_grams: 100,
          cost: 2,
          time_unknown: false,
        },
        {
          plate_id: 200,
          library_file_id: 6,
          plate_index: 0,
          filename: 'small.3mf',
          count: 1,
          useful: [{ part_id: 1, name: 'Body', count: 2 }],
          print_time_seconds: 1800,
          filament_used_grams: 20,
          cost: 0.4,
          time_unknown: false,
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
  totals: { prints: 2, print_time_seconds: 5400, filament_used_grams: 120, cost: 2.4 },
};

const plates: PlateRecipe[] = [
  {
    id: 100,
    library_file_id: 5,
    plate_index: 1,
    filename: 'big.3mf',
    sliced: true,
    yield: [{ part_id: 1, name: 'Body', count: 10 }],
    unassigned: [],
    materials: ['PETG'],
    colors: [],
    print_time_seconds: 3600,
    filament_used_grams: 100,
  },
  {
    id: 200,
    library_file_id: 6,
    plate_index: 0,
    filename: 'small.3mf',
    sliced: true,
    yield: [{ part_id: 1, name: 'Body', count: 6 }],
    unassigned: [],
    materials: ['PETG'],
    colors: [],
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
  sliced: true,
  yield: [{ part_id: 1, name: 'Body', count: 3 }],
  unassigned: [],
  materials: ['PETG'],
  colors: [],
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
  count: 2,
  useful: [{ part_id: 1, name: 'Body', count: 3 }],
  print_time_seconds: 900,
  filament_used_grams: 50,
  cost: 1,
  time_unknown: false,
};

describe('PlanBlock', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    printModal.props = null;
    auth.granted = new Set(['projects:update', 'queue:create', 'printers:control']);
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue(plan);
    vi.spyOn(api, 'getProductPlates').mockResolvedValue(plates);
    // The currency is the test's choice, not `formatMoney`'s USD fallback —
    // otherwise the money assertions pass on an unresolved settings query.
    vi.spyOn(api, 'getSettings').mockResolvedValue({ currency: 'UAH' } as never);
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
    await waitFor(() => expect(api.getOrderPlan).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onProbeFetch).toHaveBeenCalledTimes(2));
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

    expect(await screen.findByTestId('plan-closed')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100')).not.toBeInTheDocument();
    expect(api.getOrderPlan).not.toHaveBeenCalled();
  });

  it('says so when there is nothing outstanding left to plan', async () => {
    vi.spyOn(api, 'getOrderPlan').mockResolvedValue({
      lines: [{ ...plan.lines[0], rows: [], outstanding_before: [], unsatisfiable: [], surplus_after: [] }],
      totals: { prints: 0, print_time_seconds: 0, filament_used_grams: 0, cost: null },
    });

    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-enqueue-all')).not.toBeInTheDocument();
  });

  it('keeps the block and offers a retry when the plan cannot be loaded', async () => {
    // ⚠️ A block that returns null on a failed fetch is simply absent from the
    // page, which reads as "this order has nothing to print" — the one thing a
    // failed plan must not say.
    const get = vi.spyOn(api, 'getOrderPlan').mockRejectedValue(new Error('Gateway timeout'));

    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-error')).toHaveTextContent(/could not load the plan/i);
    expect(screen.getByRole('heading', { name: /what to print next/i })).toBeInTheDocument();

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
    auth.granted = new Set(['projects:update', 'printers:control']);
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-printer')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-queue')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-enqueue-all')).not.toBeInTheDocument();

    cleanup();
    auth.granted = new Set(['projects:update', 'queue:create']);
    render(<PlanBlock order={order} canEdit />);

    expect(await screen.findByTestId('plan-row-10-100-queue')).toBeInTheDocument();
    expect(screen.getByTestId('plan-enqueue-all')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-printer')).not.toBeInTheDocument();
  });

  it('hides every queue action from a reader, and keeps the printer', async () => {
    render(<PlanBlock order={order} canEdit={false} />);

    expect(await screen.findByTestId('plan-row-10-100')).toBeInTheDocument();
    expect(screen.queryByTestId('plan-enqueue-all')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-row-10-100-queue')).not.toBeInTheDocument();
    // ⚠️ `canEdit` is the ORDER's own gate (a closed order, a read-only view)
    // and it does not speak for the printers. Somebody holding
    // `printers:control` may still send a plate to a machine.
    expect(screen.getByTestId('plan-row-10-100-printer')).toBeInTheDocument();
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

  it('reseeds the counts when the plan itself changed', async () => {
    const get = vi.spyOn(api, 'getOrderPlan').mockResolvedValue(plan);

    render(
      <>
        <PlanBlock order={order} canEdit />
        <Refetcher id={1} />
      </>,
    );

    fireEvent.change(await screen.findByTestId('plan-row-10-100-count'), { target: { value: '5' } });
    expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(5);

    // Six bodies outstanding instead of twelve: an edit made against the old
    // plan has nothing left to mean, so the server's counts come back.
    get.mockResolvedValue({
      ...plan,
      lines: [{ ...plan.lines[0], outstanding_before: [{ part_id: 1, name: 'Body', count: 6 }] }],
    });
    fireEvent.click(screen.getByTestId('force-refetch'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(screen.getByTestId('plan-row-10-100-count')).toHaveValue(1));
  });

  it('drops a hand-added plate once the server plans it, rather than showing it twice', async () => {
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

    await waitFor(() => expect(screen.getByTestId('plan-row-10-300-count')).toHaveValue(2));
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
    auth.granted = new Set(['projects:update', 'printers:control']);
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

  it('heads the plate table with named columns', async () => {
    render(<PlanBlock order={order} canEdit />);

    const headers = within(await screen.findByTestId('plan-line-10')).getAllByRole('columnheader');
    expect(headers.map((h) => h.textContent)).toEqual([
      'Plate',
      'Covers',
      'Prints',
      'Print time',
      'Filament, g',
      'Cost',
      'Actions',
    ]);
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
    expect(printModal.props?.preselectedPlateId).toBeUndefined();
  });
});
