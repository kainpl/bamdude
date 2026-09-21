/**
 * The print dialog's Quantity on several printers: per printer (the field's
 * old meaning) or a total dealt round-robin (spec 2026-09-11). What is pinned:
 * when the toggle shows, what each mode posts to each printer, that a printer
 * dealt nothing gets no request, that the plan line says what will happen,
 * that the choice survives a remount, and that a group answer carries it.
 */
import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import {
  DEFAULT_AUTO_MODE_OPTIONS,
  DEFAULT_PRINT_OPTIONS,
  DEFAULT_SCHEDULE_OPTIONS,
  DEFAULT_SWAP_MACROS_OPTIONS,
  QUANTITY_MODE_STORAGE_KEY,
  type PrintModalAnswer,
} from '../../components/PrintModal/types';

const printers = [
  { id: 1, name: 'A1-01', model: 'A1', ip_address: '192.168.1.101', enabled: true, is_active: true },
  { id: 2, name: 'A1-02', model: 'A1', ip_address: '192.168.1.102', enabled: true, is_active: true },
  { id: 3, name: 'A1-03', model: 'A1', ip_address: '192.168.1.103', enabled: true, is_active: true },
];

const statusWithPetg = {
  connected: true,
  state: 'IDLE',
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: 'PETG', tray_color: 'FF0000FF', remain: 90 },
        { id: 1, tray_type: '', tray_color: '', remain: -1 },
        { id: 2, tray_type: '', tray_color: '', remain: -1 },
        { id: 3, tray_type: '', tray_color: '', remain: -1 },
      ],
    },
  ],
  vt_tray: [],
};

describe('quantity mode', () => {
  let posts: Array<{ queue_id: number; quantity: number }>;
  let onAnswered: Mock<(a: PrintModalAnswer) => void>;

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    posts = [];
    onAnswered = vi.fn<(a: PrintModalAnswer) => void>();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWithPetg)),
      http.get('/api/v1/archives/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] }),
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () =>
        HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] }),
      ),
      http.post('/api/v1/queue/', async ({ request }) => {
        const body = (await request.json()) as { queue_id: number; quantity: number };
        posts.push({ queue_id: body.queue_id, quantity: body.quantity });
        return HttpResponse.json({ id: posts.length, status: 'pending', created_item_ids: [posts.length] });
      }),
    );
  });

  const mount = (printerIds: number[], seededAnswer?: PrintModalAnswer) =>
    render(
      <PrintModal
        mode="add-to-queue"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={printerIds}
        seededAnswer={seededAnswer}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
        onAnswered={onAnswered}
      />,
    );

  /** A leader's answer, as `reportAnswer` builds one. */
  const answer = (over: Partial<PrintModalAnswer> = {}): PrintModalAnswer => ({
    selectedPrinterIds: [1, 2, 3],
    dispatchMode: 'specific',
    autoModeOptions: DEFAULT_AUTO_MODE_OPTIONS,
    scheduleOptions: DEFAULT_SCHEDULE_OPTIONS,
    quantity: 1,
    quantityMode: 'perPrinter',
    printOptions: DEFAULT_PRINT_OPTIONS,
    swapMacros: DEFAULT_SWAP_MACROS_OPTIONS,
    selectedMacroIds: [],
    ...over,
  });

  const setQuantity = (n: number) => {
    const input = screen.getByLabelText('Quantity') as HTMLInputElement;
    fireEvent.change(input, { target: { value: String(n) } });
  };

  // The submit button renames itself once more than one printer is picked
  // ("Queue to 3 Printers"), which is exactly the case every test here is
  // about — so the matcher has to accept both wordings.
  const submit = () =>
    fireEvent.click(screen.getByRole('button', { name: /^(add to queue|queue to \d+ printers)$/i }));

  it('shows no toggle with one printer and a toggle with several', async () => {
    mount([1]);
    await screen.findByLabelText('Quantity');
    expect(screen.queryByTestId('quantity-mode-toggle')).toBeNull();
  });

  it('total 13 on three printers posts 5 / 4 / 4 in list order', async () => {
    mount([1, 2, 3]);
    await screen.findByTestId('quantity-mode-toggle');
    fireEvent.click(screen.getByTestId('quantity-mode-total'));
    setQuantity(13);
    await waitFor(() =>
      expect(screen.getByTestId('quantity-plan')).toHaveTextContent('13 → A1-01: 5 · A1-02: 4 · A1-03: 4'),
    );
    submit();
    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts).toEqual([
      { queue_id: 1, quantity: 5 },
      { queue_id: 2, quantity: 4 },
      { queue_id: 3, quantity: 4 },
    ]);
  });

  it('per printer 4 on three printers posts 4 to each, and says so', async () => {
    mount([1, 2, 3]);
    await screen.findByTestId('quantity-mode-toggle');
    expect(screen.getByTestId('quantity-mode-perPrinter')).toHaveAttribute('aria-pressed', 'true');
    setQuantity(4);
    await waitFor(() => expect(screen.getByTestId('quantity-plan')).toHaveTextContent('4 × 3 printers = 12 in total'));
    submit();
    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts.map((p) => p.quantity)).toEqual([4, 4, 4]);
  });

  it('a printer dealt nothing gets no request', async () => {
    mount([1, 2, 3]);
    await screen.findByTestId('quantity-mode-toggle');
    fireEvent.click(screen.getByTestId('quantity-mode-total'));
    setQuantity(2);
    await waitFor(() => expect(screen.getByTestId('quantity-plan')).toHaveTextContent('A1-03: 0'));
    submit();
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts).toEqual([
      { queue_id: 1, quantity: 1 },
      { queue_id: 2, quantity: 1 },
    ]);
  });

  it('remembers the choice in the browser and reads it back', async () => {
    const first = mount([1, 2]);
    await screen.findByTestId('quantity-mode-toggle');
    fireEvent.click(screen.getByTestId('quantity-mode-total'));
    expect(localStorage.getItem(QUANTITY_MODE_STORAGE_KEY)).toBe('total');
    first.unmount();
    mount([1, 2]);
    await screen.findByTestId('quantity-mode-toggle');
    expect(screen.getByTestId('quantity-mode-total')).toHaveAttribute('aria-pressed', 'true');
  });

  it('deals in the selector LIST order however the printers were ticked', async () => {
    // ⚠️ The deal walks the printers in the order `/printers/` LISTS them,
    // while the requests go out in the order they were ticked. Every other
    // case here mounts [1, 2, 3] against a list of [1, 2, 3], where the two
    // orders are the same sequence and no assertion can tell them apart — an
    // implementation that dealt in tick order would pass them all unchanged.
    // Ticked backwards, printer 1 takes the tail (5) because it is FIRST IN
    // THE LIST, and the POSTs still arrive in tick order.
    mount([3, 1, 2]);
    await screen.findByTestId('quantity-mode-toggle');
    fireEvent.click(screen.getByTestId('quantity-mode-total'));
    setQuantity(13);
    await waitFor(() =>
      expect(screen.getByTestId('quantity-plan')).toHaveTextContent('13 → A1-01: 5 · A1-02: 4 · A1-03: 4'),
    );
    submit();
    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts).toEqual([
      { queue_id: 3, quantity: 4 },
      { queue_id: 1, quantity: 5 },
      { queue_id: 2, quantity: 4 },
    ]);
  });

  it("a seeded answer's mode beats the browser's memory", async () => {
    // A silent group member splits the way its LEADER did (spec §4). The
    // browser here remembers the other mode, so only the seed can produce
    // 5 / 4 / 4 — and if the field ever went missing from an answer, this is
    // the half that would fail silently, splitting a member 13 × 3.
    localStorage.setItem(QUANTITY_MODE_STORAGE_KEY, 'perPrinter');
    mount([1, 2, 3], answer({ quantity: 13, quantityMode: 'total' }));
    await screen.findByTestId('quantity-mode-toggle');
    expect(screen.getByTestId('quantity-mode-total')).toHaveAttribute('aria-pressed', 'true');
    submit();
    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts).toEqual([
      { queue_id: 1, quantity: 5 },
      { queue_id: 2, quantity: 4 },
      { queue_id: 3, quantity: 4 },
    ]);
  });

  it('gives every selected plate its own line, with the cursor carrying between them', async () => {
    // Spec §5: in total mode each plate's number is a total of its own, so
    // each gets a line — and the deal carries its cursor from one plate to the
    // next, which is why plate 2 does not start at A1-01 again. The plates are
    // unnamed, so the line's «Plate N» comes from the same i18n fallback the
    // plate picker above it uses.
    server.use(
      http.get('/api/v1/archives/:id/plates', () =>
        HttpResponse.json({
          is_multi_plate: true,
          plates: [
            { index: 1, name: null, objects: [], filaments: [] },
            { index: 2, name: null, objects: [], filaments: [] },
          ],
        }),
      ),
    );
    mount([1, 2, 3]);
    await screen.findByTestId('quantity-mode-toggle');
    fireEvent.click(screen.getByTestId('quantity-mode-total'));
    setQuantity(5);
    fireEvent.click(screen.getByRole('button', { name: /select all 2 plates/i }));

    await waitFor(() =>
      expect(screen.getByTestId('quantity-plan-0')).toHaveTextContent('Plate 1: 5 → A1-01: 2 · A1-02: 2 · A1-03: 1'),
    );
    expect(screen.getByTestId('quantity-plan-1')).toHaveTextContent('Plate 2: 5 → A1-01: 2 · A1-02: 1 · A1-03: 2');

    // What the lines say is what is sent: two plates × three printers, each
    // request carrying its own cell of the deal.
    fireEvent.click(screen.getByRole('button', { name: /^queue 2 plates$/i }));
    await waitFor(() => expect(posts).toHaveLength(6));
    expect(posts).toEqual([
      { queue_id: 1, quantity: 2 },
      { queue_id: 2, quantity: 2 },
      { queue_id: 3, quantity: 1 },
      { queue_id: 1, quantity: 2 },
      { queue_id: 2, quantity: 1 },
      { queue_id: 3, quantity: 2 },
    ]);
  });

  it('the group answer carries the mode', async () => {
    mount([1, 2]);
    await screen.findByTestId('quantity-mode-toggle');
    fireEvent.click(screen.getByTestId('quantity-mode-total'));
    setQuantity(3);
    await waitFor(() => expect(screen.getByRole('button', { name: /^(add to queue|queue to \d+ printers)$/i })).toBeEnabled());
    submit();
    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswered.mock.calls[0][0].quantityMode).toBe('total');
  });
  /**
   * ⚠️ A retry asks for what is MISSING, not for the total again.
   *
   * Once a partial refusal unticks the printers that already took work, the
   * dialog invites a second press in so many words — and in `total` mode the
   * field is one number for the whole submit. Left at 10 after 4 copies landed,
   * that press orders 10 MORE across the two printers that refused and the farm
   * over-produces by four. The arithmetic was always this way; nothing used to
   * tell the operator to press again.
   */
  it('a retry after a partial refusal asks for the copies still missing', async () => {
    server.use(
      http.post('/api/v1/queue/', async ({ request }) => {
        const body = (await request.json()) as { queue_id: number; quantity: number };
        posts.push({ queue_id: body.queue_id, quantity: body.quantity });
        if (body.queue_id === 1) {
          return HttpResponse.json({ id: posts.length, status: 'pending', created_item_ids: [posts.length] });
        }
        return HttpResponse.json(
          { detail: { code: 'source_copy_busy', params: {}, message: 'busy' } },
          { status: 503 },
        );
      }),
    );
    mount([1, 2, 3]);
    await screen.findByTestId('quantity-mode-toggle');
    fireEvent.click(screen.getByTestId('quantity-mode-total'));
    setQuantity(10);
    await waitFor(() =>
      expect(screen.getByTestId('quantity-plan')).toHaveTextContent('10 → A1-01: 4 · A1-02: 3 · A1-03: 3'),
    );

    submit();
    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts.map((p) => p.quantity)).toEqual([4, 3, 3]);

    // Four landed on A1-01. The field now reads the six that did not, and the
    // plan re-deals those six over the two printers still ticked.
    await waitFor(() => expect((screen.getByLabelText('Quantity') as HTMLInputElement).value).toBe('6'));
    await waitFor(() =>
      expect(screen.getByTestId('quantity-plan')).toHaveTextContent('6 → A1-02: 3 · A1-03: 3'),
    );

    fireEvent.click(screen.getByRole('button', { name: /queue to 2 printers/i }));
    await waitFor(() => expect(posts).toHaveLength(5));
    // Not 10 again, and not on A1-01.
    expect(posts.slice(3)).toEqual([
      { queue_id: 2, quantity: 3 },
      { queue_id: 3, quantity: 3 },
    ]);
  });
});
