/**
 * The order manager of WS-13 E13 O05 — `orders:*`, `products:read`, `customers:read`,
 * `customers:create`, no stock right at all. Every order door that moves the shelf asks
 * `stock:move` (O06/O23) and every shelf figure `stock:read`, so this role is offered none of
 * them and asks for none of them — it adds lines that take nothing, completes an order whose
 * goods are already out, and never meets a 403 it could not have avoided (O19). Completing is
 * a consequence of the order's own right (O23), never a movement.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../utils';
import { api } from '../../api/client';
import type { FulfilmentState, Order, Permission, StockIssueRow } from '../../api/client';
import { AddToOrderDialog } from '../../components/projects/add-to-order/AddToOrderDialog';
import { orderMenuItems } from '../../components/projects/orderActions/orderMenu';
import { TakeStockBanner } from '../../components/projects/TakeStockBanner';
import { CloseSuggestionBanner } from '../../components/projects/CloseSuggestionBanner';
import { FulfilmentDialog } from '../../components/projects/fulfilment/FulfilmentDialog';
import { LineEditDialog } from '../../components/projects/LineEditDialog';
import { DispatchNotesSection } from '../../components/stock/DispatchNotesSection';
import { canIssue, canTakeStock, canWriteOff } from '../../utils/workshopRights';
import { libraryFile, filesPage, pageOf, pipe, pipeDetail, plate } from '../components/projects/add-to-order/fixtures';
import { makeLine, makeOrder } from '../fixtures/orderDetail';

const auth = vi.hoisted(() => ({ granted: new Set<string>() }));
vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      ...actual.useAuth(),
      hasPermission: (p: Permission) => auth.granted.has(p),
      hasAnyPermission: (...ps: Permission[]) => ps.some((p) => auth.granted.has(p)),
    }),
  };
});

const MANAGER = [
  'orders:read',
  'orders:create',
  'orders:update',
  'orders:delete',
  'orders:file_prints',
  'products:read',
  'customers:read',
  'customers:create',
];
const has = (p: string) => auth.granted.has(p);

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  auth.granted = new Set(MANAGER);
});

describe('an order manager — orders:*, products:read, customers:read + create, no stock', () => {
  it('the rights helpers: no taking, no issuing, no write-off', () => {
    expect(canTakeStock(has)).toBe(false);
    expect(canIssue(has)).toBe(false);
    expect(canWriteOff(has)).toBe(false);
    auth.granted = new Set([...MANAGER, 'stock:move']);
    expect(canTakeStock(has)).toBe(true);
    expect(canIssue(has)).toBe(true);
    expect(canWriteOff(has)).toBe(false);
  });

  it('the order menu offers completing, never issuing or banking', () => {
    const actions = orderMenuItems(
      { status: 'active', bankable_surplus: 3 },
      { update: true, create: true, remove: true, issue: false },
      'detail',
    ).map((i) => i.action);
    expect(actions).toContain('complete');
    expect(actions).not.toContain('fulfil');
    expect(actions).not.toContain('bank');
  });

  it('adds a product and a plate taking nothing from the shelf, and asks no proposal', async () => {
    // The one-off tab lists the library's files: this manager reads the library too.
    auth.granted = new Set([...MANAGER, 'library:read_all']);
    vi.spyOn(api, 'getProductsPaged').mockResolvedValue(pageOf([pipe], 1));
    vi.spyOn(api, 'getProduct').mockResolvedValue(pipeDetail as never);
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: [] });
    vi.spyOn(api, 'getProductParts').mockResolvedValue({ items: [], meta: { total: 0, current_page: 1, per_page: 24, last_page: 1 } });
    vi.spyOn(api, 'getLibraryFilesPaged').mockResolvedValue(filesPage([libraryFile(31, 'flask.gcode.3mf')]) as never);
    vi.spyOn(api, 'getLibraryFilePlates').mockResolvedValue({ file_id: 31, filename: 'flask.gcode.3mf', plates: [plate(1)], is_multi_plate: false });
    const suggest = vi.spyOn(api, 'suggestStock');
    // The combination's state is the product's read (its route's gate, owner 2026-11-14): this
    // manager, without any stock right, still gets the marks.
    const describeCombination = vi.spyOn(api, 'describeCombination').mockResolvedValue({
      available: true,
      reasons: [],
      options: [
        { group_id: 10, option_id: 100, available_here: true, available_anywhere: true },
        { group_id: 10, option_id: 101, available_here: false, available_anywhere: true },
      ],
    });
    const add = vi.spyOn(api, 'addOrderLines').mockImplementation(async (_order, lines) => ({
      order: { id: 5 } as never,
      results: lines.map((_line, i) => ({ line_id: 40 + i, asked_finished: 0, got_finished: 0, asked_kits: 0, got_kits: 0 })),
    }));
    render(<AddToOrderDialog order={{ id: 5, code: 'OR-0005', name: 'Flasks', active: true }} onClose={() => {}} />);
    const row = await screen.findByTestId('add-product-1');
    fireEvent.click(within(row).getByRole('checkbox'));
    expect(await within(row).findByRole('option', { name: 'angled — not with this choice' })).toBeEnabled();
    expect(describeCombination).toHaveBeenCalledWith(1, {});
    fireEvent.click(screen.getByRole('tab', { name: 'One-off from a file' }));
    fireEvent.click(await screen.findByRole('button', { name: /^flask\.gcode\.3mf/ }));
    fireEvent.click(await screen.findByRole('radio', { name: /Plate 1/ }));
    const submit = screen.getByRole('button', { name: 'Add lines (2)' });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    const none = { from_finished: 0, from_kits: 0 };
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(5, [
        expect.objectContaining({ kind: 'product', product_id: 1, stock: none }),
        { kind: 'plate', library_file_id: 31, plate_index: 1, copies: 1, stock: none },
      ]),
    );
    expect(suggest).not.toHaveBeenCalled();
  });

  it('is offered no «take from stock» and asks no offers', async () => {
    const offers = vi.spyOn(api, 'getStockOffers').mockResolvedValue([]);
    render(<TakeStockBanner orderId={5} />);
    await new Promise((r) => setTimeout(r, 30));
    expect(offers).not.toHaveBeenCalled();
  });

  it('the close banner offers completing, not receiving or issuing', () => {
    const order = { id: 1, status: 'active', figures: { all_printed: true } } as unknown as Order;
    const state = {
      lines: [],
      ordered: 10,
      issued: 4,
      held: 6,
      fully_issued: false,
      can_assemble: 0,
      can_receive: 3,
      can_issue: 9,
      closes_to_stock: false,
      can_complete: false,
      recipient: { name: null, phone: null, delivery_method: null, delivery_details: null },
    } as FulfilmentState;
    render(<CloseSuggestionBanner order={order} state={state} onFulfil={() => {}} />);
    expect(screen.queryByTestId('close-suggestion-receive')).toBeNull();
    expect(screen.queryByTestId('close-suggestion-issue')).toBeNull();
    expect(screen.getByTestId('close-suggestion-complete')).toBeInTheDocument();
  });

  it('completes an order whose goods are out, through the dialog, moving nothing', async () => {
    vi.spyOn(api, 'getFulfilment').mockResolvedValue({
      lines: [
        {
          line_id: 7,
          product_name: 'Pipe',
          mode: 'product',
          ordered: 2,
          from_finished: 0,
          kits_reserved: 0,
          can_assemble: 1,
          can_receive: 1,
          held: 0,
          issued: 2,
          written_off: 0,
          parts: [],
          configuration: null,
          stock_position: null,
        },
      ],
      ordered: 2,
      issued: 2,
      held: 0,
      fully_issued: true,
      can_assemble: 1,
      can_receive: 1,
      can_issue: 0,
      closes_to_stock: false,
      can_complete: true,
      recipient: { name: null, phone: null, delivery_method: null, delivery_details: null },
    });
    vi.spyOn(api, 'getDeliveryMethods').mockResolvedValue([]);
    const fulfil = vi
      .spyOn(api, 'fulfilOrder')
      .mockResolvedValue({ order: { id: 5 } as never, issue_id: null, issue_code: null, issue_units: null });
    render(
      <FulfilmentDialog
        order={{ id: 5, code: 'OR-0005', name: 'Pipes', status: 'active', customer_name: 'ACME', bankable_surplus: 0, due_date: null }}
        complete
        onClose={() => {}}
      />,
    );
    await screen.findByText('Pipe');
    // Nothing to type: a movement is the stock's.
    expect(document.querySelectorAll('table input[type="number"]:not(:disabled)')).toHaveLength(0);
    expect(screen.getByText(/need the right to move stock/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Execute' }));
    await waitFor(() => expect(fulfil).toHaveBeenCalledWith(5, expect.objectContaining({ lines: [], complete: true })));
  });

  it('edits a line without the shelf: no stock figures asked, no stock fields', async () => {
    vi.spyOn(api, 'getProduct').mockResolvedValue({ id: 1, name: 'Flask', materials: [], colors: [] } as never);
    const stock = vi.spyOn(api, 'getProductStock');
    const suggest = vi.spyOn(api, 'suggestStock');
    const line = makeLine({ id: 10, product_id: 1, product_name: 'Flask', quantity: 2, from_stock_units: 0, from_finished: 0, from_kit_units: 0 });
    const order = makeOrder({ id: 1, status: 'active', lines: [line] });
    render(<LineEditDialog order={order} line={line} onClose={() => {}} onConfigure={() => {}} />);
    await screen.findByRole('dialog', { name: 'Edit line' });
    await new Promise((r) => setTimeout(r, 30));
    expect(stock).not.toHaveBeenCalled();
    expect(suggest).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('From stock — ready')).toBeNull();
    expect(screen.queryByLabelText('From stock — part kits')).toBeNull();
  });

  it('reads the order’s dispatch notes without a waybill pencil', async () => {
    const row: StockIssueRow = {
      id: 1,
      code: 'DN-0001',
      created_at: '2026-09-27T10:00:00',
      project_id: 5,
      order_code: 'OR-0005',
      order_name: 'Hall lights',
      customer_id: 2,
      customer_name: 'ACME',
      units: 4,
      lines_count: 1,
      summary: [],
      recipient_name: 'Ivan',
      recipient_phone: '+380',
      delivery_method: null,
      delivery_details: null,
      waybill: null,
      note: null,
      created_by_name: 'olena',
    };
    vi.spyOn(api, 'getDispatchNotes').mockResolvedValue({ items: [row], meta: { total: 1, current_page: 1, per_page: 24, last_page: 1 } });
    render(<DispatchNotesSection projectId={5} canEdit inTab />);
    await screen.findByTestId('note-1');
    expect(screen.queryByRole('button', { name: /edit the waybill/i })).toBeNull();
  });
});
