/**
 * Grouping comes from the RESPONSE, not from the archives.
 *
 * `lines[].archive_ids` is not a partition — one print can count against two
 * lines at once (a plate that carries parts of both), so the same archive is
 * expected under two headings and an id-set walk over the archives would show
 * it under neither. Whatever no line claimed lands under "other prints" from
 * `other_archive_ids`; the leftover group exists only as a defensive net.
 *
 * The READ: the order names every archive it counts, so the page keeps asking for
 * pages until it holds them all — and when its guard stops the walk short, the
 * groups say they are counted over what was LOADED (WS-13 E4 F02, R02).
 *
 * WS-13 E4 F: the card (plate, printer, date and time), a page per group, the
 * menu gated as the server gates it (R03), «File under a line» as a dialog, and a
 * confirmation before a print leaves the order.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order, Permission } from '../../../api/client';
import { OrderPrints } from '../../../components/projects/OrderPrints';
import { strayZeroTextNodes } from '../../domHelpers';
import { formatDateTime } from '../../../utils/date';

/** What the mocked `useAuth` grants — reset in `beforeEach`, narrowed per test. */
const auth = vi.hoisted(() => ({ granted: new Set<string>(), userId: 5 }));

// The menu's «File under a line» follows the ARCHIVE's owner (R03) and the printer
// names need `printers:read`; the real provider always resolves one admin, so only
// the hook is replaced, with the provider's own ownership rule.
vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      ...actual.useAuth(),
      hasPermission: (p: Permission) => auth.granted.has(p),
      canModify: (resource: string, action: string, createdById: number | null | undefined) => {
        if (auth.granted.has(`${resource}:${action}_all`)) return true;
        if (auth.granted.has(`${resource}:${action}_own`)) return createdById != null && createdById === auth.userId;
        return false;
      },
    }),
  };
});

/** The card writes day, month and time — the other order views' format. */
const PRINT_WHEN: Intl.DateTimeFormatOptions = { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' };

/** `id` → a minimal archive row, the shape `getProjectArchives` answers with. */
function rows(ids: number[], lineId: number | null = 10) {
  return ids.map((id) => ({
    id,
    filename: `p${id}.3mf`,
    status: 'completed',
    project_line_id: lineId,
  }));
}

function lineOrder(ids: number[], over: Record<string, unknown> = {}) {
  return {
    id: 1,
    other_archive_ids: [],
    lines: [{ id: 10, product_name: 'Flask', quantity: 2, mode: 'product', archive_ids: ids, ...over }],
  } as unknown as Order;
}

const cardOf = (name: string) => screen.getByText(name).closest('[data-print-card]') as HTMLElement;

async function openMenu(id: number) {
  fireEvent.click(await screen.findByTestId(`print-menu-${id}`));
  return screen.findByRole('menu');
}

describe('OrderPrints', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'archives:update_all', 'printers:read']);
    vi.spyOn(api, 'getPrintersWithArchived').mockResolvedValue([
      { id: 3, name: 'P1S-02', model: 'P1S' },
    ] as never);
  });

  it('renders restricted archive accounting without a filename, thumbnail or archive/library link', async () => {
    const thumbnail = vi.spyOn(api, 'getArchiveThumbnail');
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([{
      id: 31, restricted: true, status: 'completed', project_id: 1, project_line_id: 10,
      printer_id: 3, plate_index: 2, quantity: 4, defective_count: 1,
      created_at: '2026-11-10T12:00:00', started_at: null, completed_at: null, created_by_id: 5,
    }] as never);
    render(<OrderPrints order={lineOrder([31])} canEdit />);
    const title = await screen.findByText('Restricted print #31');
    const card = title.closest<HTMLElement>('[data-print-card]')!;
    expect(card).toHaveAttribute('data-status', 'completed');
    expect(card).toHaveTextContent('P1S-02');
    expect(card).toHaveTextContent('4 pcs');
    expect(card).toHaveTextContent('1 defective');
    expect(card.querySelector('a')).toBeNull();
    expect(card.querySelector('img')).toBeNull();
    expect(thumbnail).not.toHaveBeenCalled();
    expect(card).not.toHaveTextContent('undefined');
    // The order's accounting action remains available under its own permission.
    expect(within(card).getByTestId('print-menu-31')).toBeInTheDocument();
  });

  it('groups archives by line from the response and lists the leftovers as other prints', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: 10 },
      { id: 2, filename: 'b.3mf', status: 'completed', project_line_id: null },
      { id: 3, filename: 'c.3mf', status: 'completed', project_line_id: null },
    ] as never);

    const order = {
      id: 1,
      other_archive_ids: [3],
      lines: [
        { id: 10, product_name: 'Flask', quantity: 2, mode: 'product', archive_ids: [1, 2] },
        { id: 11, product_name: 'Lid', quantity: 1, mode: 'parts', archive_ids: [2] },
      ],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    const flask = await screen.findByTestId('prints-line-10');
    expect(flask.textContent).toContain('a.3mf');
    expect(flask.textContent).toContain('b.3mf');
    // One archive, two lines — grouping is not a partition.
    expect(screen.getByTestId('prints-line-11').textContent).toContain('b.3mf');
    expect(screen.getByTestId('prints-other').textContent).toContain('c.3mf');
    expect(screen.getAllByText(/attributed/i).length).toBeGreaterThan(0);
  });

  it('heads each group with its line and how many prints it holds', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([...rows([1, 2]), ...rows([3], 11), ...rows([4], null)] as never);
    const order = {
      id: 1,
      other_archive_ids: [4],
      lines: [
        { id: 10, product_name: 'Flask', quantity: 2, mode: 'product', archive_ids: [1, 2] },
        { id: 11, product_name: 'Lid', quantity: 1, mode: 'parts', archive_ids: [3] },
      ],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    const heading = (id: string) => within(screen.getByTestId(id)).getByRole('heading', { level: 3 });
    expect((await screen.findByTestId('prints-line-10')).querySelector('h3')).toHaveTextContent('Flask — × 2 2');
    expect(within(heading('prints-line-10')).getByText('2')).toBeInTheDocument();
    expect(heading('prints-line-11')).toHaveTextContent('Lid — parts 1');
    expect(heading('prints-other')).toHaveTextContent('Other prints 1');
  });

  it('says which of two prints under the same line was filed by hand', async () => {
    // The badge reads `archive.project_line_id`, NOT the group the card is drawn in.
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'by-hand.3mf', status: 'completed', project_line_id: 10 },
      { id: 2, filename: 'by-the-server.3mf', status: 'completed', project_line_id: null },
    ] as never);

    render(<OrderPrints order={lineOrder([1, 2])} canEdit />);

    const group = await screen.findByTestId('prints-line-10');
    expect(within(cardOf('by-hand.3mf')).getByText('Filed')).toBeInTheDocument();
    expect(within(cardOf('by-the-server.3mf')).getByText('Attributed')).toBeInTheDocument();
    expect(within(cardOf('by-hand.3mf')).getByText('Filed')).toHaveAttribute('title', 'Flask');
    expect(within(cardOf('by-the-server.3mf')).getByText('Attributed')).not.toHaveAttribute('title');
    expect(group.textContent).toContain('by-hand.3mf');
    expect(group.textContent).toContain('by-the-server.3mf');
  });

  it('shows an archive no group claimed rather than dropping it', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 7, filename: 'stray.3mf', status: 'completed', project_line_id: null },
    ] as never);
    render(<OrderPrints order={lineOrder([])} canEdit />);
    expect((await screen.findByTestId('prints-unlisted')).textContent).toContain('stray.3mf');
  });

  it('links a print to its library file when there is one, and to the name alone otherwise', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'linked.3mf', status: 'completed', project_line_id: 10, library_file_id: 42 },
      { id: 2, filename: 'external print.3mf', status: 'completed', project_line_id: 10, library_file_id: null },
    ] as never);

    render(<OrderPrints order={lineOrder([1, 2])} canEdit />);

    const group = await screen.findByTestId('prints-line-10');
    const hrefs = Array.from(group.querySelectorAll('a')).map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/archives?file=42&fileName=linked.3mf');
    expect(hrefs).toContain('/archives');
    expect(hrefs).not.toContain('/archives?fileName=external%20print.3mf');
  });

  it('says the plate, the printer and when, on the card', async () => {
    const when = '2026-09-28T09:42:00Z';
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: 10, plate_index: 2, printer_id: 3, completed_at: when, quantity: 6, defective_count: 1 },
      { id: 2, filename: 'b.3mf', status: 'printing', project_line_id: null, plate_index: 0, printer_id: 99, started_at: when },
    ] as never);

    render(<OrderPrints order={lineOrder([1, 2])} canEdit />);

    await screen.findByText('a.3mf');
    const first = cardOf('a.3mf');
    await waitFor(() => expect(within(first).getByTestId('print-where-1')).toHaveTextContent('P1S-02'));
    expect(within(first).getByTestId('print-where-1')).toHaveTextContent(`plate 2 · P1S-02 · ${formatDateTime(when, 'system', 'system', PRINT_WHEN)}`);
    expect(screen.getByTestId('print-defects-1')).toHaveTextContent('6 pcs · 1 defective');
    // An unknown printer id is skipped, never shown as a number; a whole-file print says no plate.
    expect(within(cardOf('b.3mf')).getByTestId('print-where-2')).toHaveTextContent(new RegExp(`^${formatDateTime(when, 'system', 'system', PRINT_WHEN)}$`));
    expect(within(cardOf('b.3mf')).getByText('printing')).toBeInTheDocument();
  });

  it('writes the year on a card of a print from another year (final review M6)', async () => {
    const lastYear = `${new Date().getFullYear() - 1}-03-05T09:42:00Z`;
    const thisYear = `${new Date().getFullYear()}-01-02T09:42:00Z`;
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'old.3mf', status: 'completed', project_line_id: 10, plate_index: 0, completed_at: lastYear },
      { id: 2, filename: 'new.3mf', status: 'completed', project_line_id: 10, plate_index: 0, completed_at: thisYear },
    ] as never);

    render(<OrderPrints order={lineOrder([1, 2])} canEdit />);

    await screen.findByText('old.3mf');
    expect(screen.getByTestId('print-where-1')).toHaveTextContent(
      formatDateTime(lastYear, 'system', 'system', { ...PRINT_WHEN, year: 'numeric' }),
    );
    expect(screen.getByTestId('print-where-1')).toHaveTextContent(String(new Date().getFullYear() - 1));
    expect(screen.getByTestId('print-where-2')).toHaveTextContent(formatDateTime(thisYear, 'system', 'system', PRINT_WHEN));
    expect(screen.getByTestId('print-where-2')).not.toHaveTextContent(String(new Date().getFullYear()));
  });

  it('asks nothing about printers without the right to read them, and keeps the cards', async () => {
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'archives:update_all']);
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: 10, plate_index: 1, printer_id: 3, completed_at: '2026-09-28T09:42:00Z' },
    ] as never);

    render(<OrderPrints order={lineOrder([1])} canEdit />);

    expect(await screen.findByText('a.3mf')).toBeInTheDocument();
    expect(api.getPrintersWithArchived).not.toHaveBeenCalled();
    expect(screen.getByTestId('print-where-1')).not.toHaveTextContent('P1S-02');
  });

  it('keeps the cards when the printer names cannot be read', async () => {
    vi.spyOn(api, 'getPrintersWithArchived').mockRejectedValue(new Error('boom'));
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1]) as never);

    render(<OrderPrints order={lineOrder([1])} canEdit />);

    expect(await screen.findByText('p1.3mf')).toBeInTheDocument();
    await waitFor(() => expect(api.getPrintersWithArchived).toHaveBeenCalled());
    expect(screen.queryByText(/could not/i)).not.toBeInTheDocument();
  });

  it('says the prints could not be read and offers to try again — not that there are none', async () => {
    const get = vi.spyOn(api, 'getProjectArchives').mockRejectedValue(new Error('Gateway timeout'));

    render(<OrderPrints order={lineOrder([1])} canEdit />);

    expect(await screen.findByText('Could not load the prints')).toBeInTheDocument();
    expect(screen.queryByText('No prints yet')).not.toBeInTheDocument();
    get.mockResolvedValue(rows([1]) as never);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('p1.3mf')).toBeInTheDocument();
  });

  it('says there are no prints only when the read succeeded with none', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([] as never);
    render(<OrderPrints order={lineOrder([])} canEdit />);
    expect(await screen.findByText('No prints yet')).toBeInTheDocument();
  });

  describe('a page per group (F02, R02)', () => {
    it('shows 24 of a group’s prints with the group’s own pager, and the rest on the next page', async () => {
      const all = rows(Array.from({ length: 30 }, (_, i) => i + 1));
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue(all as never);

      render(<OrderPrints order={lineOrder(all.map((a) => a.id))} canEdit />);

      const group = await screen.findByTestId('prints-line-10');
      expect(group.querySelectorAll('[data-print-card]')).toHaveLength(24);
      expect(within(group).getByText('Showing 1-24 of 30 prints')).toBeInTheDocument();
      fireEvent.click(within(group).getByRole('button', { name: /next page/i }));
      expect(group.querySelectorAll('[data-print-card]')).toHaveLength(6);
      expect(within(group).getByText('Showing 25-30 of 30 prints')).toBeInTheDocument();
    });

    it('calls a set the walk cut short «loaded», and says the order’s prints are not all here', async () => {
      const page = rows(Array.from({ length: 500 }, (_, i) => i + 1));
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue(page as never);

      render(<OrderPrints order={lineOrder([...page.map((a) => a.id), 9999])} canEdit />);

      const group = await screen.findByTestId('prints-line-10');
      expect(await within(group).findByText('Showing 1-24 of 500 loaded prints')).toBeInTheDocument();
      expect(screen.getByText('Not all of the order’s prints are loaded')).toBeInTheDocument();
      expect(screen.getByTestId('prints-load-older')).toBeInTheDocument();
    });

    it('does not take the short last page as the total when ids are missing from it', async () => {
      // A short page is the end of the history: the missing id was deleted or moved,
      // so the set is complete and says «of N», N being what arrived — not the ids named.
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1, 2]) as never);
      render(<OrderPrints order={lineOrder([1, 2, 3])} canEdit />);
      const group = await screen.findByTestId('prints-line-10');
      expect(within(group).getByText('Showing 1-2 of 2 prints')).toBeInTheDocument();
      expect(screen.queryByText('Not all of the order’s prints are loaded')).not.toBeInTheDocument();
    });

    it('clamps a group’s page when its set shrinks under it', async () => {
      const all = rows(Array.from({ length: 30 }, (_, i) => i + 1));
      const get = vi.spyOn(api, 'getProjectArchives').mockResolvedValue(all as never);

      const { rerender } = render(<OrderPrints order={lineOrder(all.map((a) => a.id))} canEdit />);
      const group = await screen.findByTestId('prints-line-10');
      fireEvent.click(within(group).getByRole('button', { name: /next page/i }));
      expect(group.querySelectorAll('[data-print-card]')).toHaveLength(6);

      // Six prints left the order: the group has one page, and page two is gone.
      get.mockResolvedValue(all.slice(0, 24) as never);
      rerender(<OrderPrints order={lineOrder(all.slice(0, 24).map((a) => a.id))} canEdit />);
      await waitFor(() => expect(screen.getByTestId('prints-line-10').querySelectorAll('[data-print-card]')).toHaveLength(24));
      expect(within(screen.getByTestId('prints-line-10')).getByText('Showing 1-24 of 24 prints')).toBeInTheDocument();

      // The clamp is kept: a print filed back in does not bring page two back by itself
      // (final review M5).
      get.mockResolvedValue(all as never);
      rerender(<OrderPrints order={lineOrder(all.map((a) => a.id))} canEdit />);
      await waitFor(() => expect(within(screen.getByTestId('prints-line-10')).getByText('Showing 1-24 of 30 prints')).toBeInTheDocument());
    });
  });

  // Codex review V03: a group that empties and disappears must not bring its old page
  // back when it fills again — the line's group and «other prints» alike.
  it.each([
    ['a line’s group', 'prints-line-10', (ids: number[]) => lineOrder(ids)],
    [
      'other prints',
      'prints-other',
      (ids: number[]) =>
        ({
          id: 1,
          other_archive_ids: ids,
          lines: [{ id: 10, product_name: 'Flask', quantity: 2, mode: 'product', archive_ids: [] }],
        }) as unknown as Order,
    ],
  ])('starts %s again at page one after it emptied (V03)', async (_name, testId, orderOf) => {
    const ids = Array.from({ length: 30 }, (_, i) => i + 1);
    const lineId = testId === 'prints-other' ? null : 10;
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows(ids, lineId) as never);
    const { rerender } = render(<OrderPrints order={orderOf(ids)} canEdit={false} />);
    const group = await screen.findByTestId(testId);
    fireEvent.click(within(group).getByRole('button', { name: /next page/i }));
    expect(within(group).getByText('Showing 25-30 of 30 prints')).toBeInTheDocument();

    rerender(<OrderPrints order={orderOf([])} canEdit={false} />);
    await waitFor(() => expect(screen.queryByTestId(testId)).not.toBeInTheDocument());
    rerender(<OrderPrints order={orderOf(ids)} canEdit={false} />);
    await waitFor(() => expect(within(screen.getByTestId(testId)).getByText('Showing 1-24 of 30 prints')).toBeInTheDocument());
  });

  it('keeps reading pages until every archive the order names is loaded', async () => {
    const all = rows(Array.from({ length: 750 }, (_, i) => i + 1));
    const getArchives = vi
      .spyOn(api, 'getProjectArchives')
      .mockImplementation((async (_id: number, limit = 500, offset = 0) => all.slice(offset, offset + limit)) as never);

    render(<OrderPrints order={lineOrder(all.map((a) => a.id))} canEdit />);

    const group = await screen.findByTestId('prints-line-10');
    await waitFor(() => expect(within(group).getByText('Showing 1-24 of 750 prints')).toBeInTheDocument());
    expect(getArchives).toHaveBeenCalledTimes(2);
    expect(getArchives).toHaveBeenNthCalledWith(1, 1, 500, 0);
    expect(getArchives).toHaveBeenNthCalledWith(2, 1, 500, 500);
    expect(screen.queryByTestId('prints-load-older')).not.toBeInTheDocument();
  });

  it('stops at its page guard and offers the rest as a button', async () => {
    const page = rows(Array.from({ length: 500 }, (_, i) => i + 1));
    const getArchives = vi.spyOn(api, 'getProjectArchives').mockResolvedValue(page as never);

    render(<OrderPrints order={lineOrder([...page.map((a) => a.id), 9999])} canEdit />);

    const button = await screen.findByTestId('prints-load-older');
    expect(getArchives).toHaveBeenCalledTimes(20);
    expect(getArchives).not.toHaveBeenCalledWith(1, 500, 20 * 500);
    fireEvent.click(button);
    await waitFor(() => expect(getArchives).toHaveBeenCalledWith(1, 500, 20 * 500));
  });

  it('keeps the button away while one page holds everything', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1]) as never);
    render(<OrderPrints order={lineOrder([1])} canEdit />);
    expect(await screen.findByTestId('prints-line-10')).toBeInTheDocument();
    expect(screen.queryByTestId('prints-load-older')).not.toBeInTheDocument();
    expect(strayZeroTextNodes(screen.getByTestId('prints-line-10'))).toHaveLength(0);
  });

  it('files a print under a line in a dialog, and sends the order with the line', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: null, plate_index: 3 },
    ] as never);
    const add = vi.spyOn(api, 'addArchivesToOrder').mockResolvedValue({} as never);
    const patch = vi.spyOn(api, 'updateArchive');
    const order = {
      id: 1,
      other_archive_ids: [1],
      lines: [
        { id: 10, product_name: 'Flask', quantity: 4, mode: 'product', archive_ids: [], configuration: { choices: [], changed_parts: [] } },
        { id: 11, product_name: 'Lid', quantity: 1, mode: 'parts', archive_ids: [] },
        // Two lines of one product told apart by their configuration (final review M7).
        {
          id: 12, product_name: 'Flask', quantity: 4, mode: 'product', archive_ids: [],
          configuration: { choices: [], changed_parts: [{ part_id: 1, name: 'body', qty: 2, standard_qty: 1 }] },
        },
        {
          id: 13, product_name: 'Flask', quantity: 2, mode: 'product', archive_ids: [],
          configuration: {
            choices: [{ group_id: 1, group_name: 'Mount', option_id: 2, option_name: 'DIN', is_default: false }],
            changed_parts: [],
          },
        },
      ],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);
    await screen.findByTestId('prints-other');

    fireEvent.click(within(await openMenu(1)).getByRole('menuitem', { name: /file under/i }));
    const dialog = await screen.findByRole('dialog', { name: 'File under a line' });
    expect(dialog).toHaveTextContent('a.3mf · plate 3');
    const select = within(dialog).getByLabelText('Line');
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'No line (other prints)',
      'Flask — × 4',
      'Lid — parts',
      'Flask — 1 part changed · × 4',
      'Flask — Mount: DIN · × 2',
    ]);
    const submit = within(dialog).getByRole('button', { name: 'File' });
    expect(submit).toBeDisabled();
    fireEvent.change(select, { target: { value: '10' } });
    fireEvent.click(submit);
    // V01: the order's command with the line — not the archive editor.
    await waitFor(() => expect(add).toHaveBeenCalledWith(1, [1], 10));
    expect(patch).not.toHaveBeenCalled();
  });

  it.each(['completed', 'cancelled'])('offers no line change in a %s order, only leaving it', async (status) => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1], null) as never);
    render(<OrderPrints order={{ ...lineOrder([]), other_archive_ids: [1], status } as Order} canEdit />);
    const menu = await openMenu(1);
    expect(within(menu).queryByRole('menuitem', { name: /file under/i })).not.toBeInTheDocument();
    expect(within(menu).getByRole('menuitem', { name: /remove from order/i })).toBeInTheDocument();
  });

  it('shows a refusal of the filing above the dialog’s footer', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1], null) as never);
    vi.spyOn(api, 'addArchivesToOrder').mockRejectedValue(new Error('That line belongs to another order'));
    render(<OrderPrints order={{ ...lineOrder([]), other_archive_ids: [1] } as Order} canEdit />);
    fireEvent.click(within(await openMenu(1)).getByRole('menuitem', { name: /file under/i }));
    const dialog = await screen.findByRole('dialog', { name: 'File under a line' });
    fireEvent.change(within(dialog).getByLabelText('Line'), { target: { value: '10' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'File' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('That line belongs to another order');
  });

  describe('who may file a print (R03)', () => {
    const three = [
      { id: 1, filename: 'mine.3mf', status: 'completed', project_line_id: 10, created_by_id: 5 },
      { id: 2, filename: 'theirs.3mf', status: 'completed', project_line_id: 10, created_by_id: 8 },
      { id: 3, filename: 'nobodys.3mf', status: 'completed', project_line_id: 10, created_by_id: null },
    ];
    const offers = async (id: number) => {
      const menu = await openMenu(id);
      const has = within(menu).queryByRole('menuitem', { name: /file under/i }) != null;
      fireEvent.keyDown(menu, { key: 'Escape' });
      return has;
    };

    it('offers it for an own print only, with «update own»', async () => {
      auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'archives:update_own']);
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue(three as never);
      render(<OrderPrints order={lineOrder([1, 2, 3])} canEdit />);
      await screen.findByText('mine.3mf');
      expect(await offers(1)).toBe(true);
      expect(await offers(2)).toBe(false);
      expect(await offers(3)).toBe(false);
    });

    it('offers it for every print with «update all»', async () => {
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue(three as never);
      render(<OrderPrints order={lineOrder([1, 2, 3])} canEdit />);
      await screen.findByText('mine.3mf');
      expect(await offers(1)).toBe(true);
      expect(await offers(2)).toBe(true);
      expect(await offers(3)).toBe(true);
    });

    // WS-13 E13 B06 (R06): taking a print out of the order rewrites the archive, so it
    // follows the same rule as filing it — and the defects, the order's own contract,
    // stay where they were.
    const menuOf = async (id: number) => {
      const menu = await openMenu(id);
      const items = within(menu).queryAllByRole('menuitem').map((item) => item.textContent);
      fireEvent.keyDown(menu, { key: 'Escape' });
      return items;
    };

    it('takes only an own print out of the order with «update own», and keeps the defects on every card', async () => {
      auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'archives:update_own']);
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue(three as never);
      render(<OrderPrints order={lineOrder([1, 2, 3])} canEdit />);
      await screen.findByText('mine.3mf');
      expect(await menuOf(1)).toEqual(['Defects…', 'File under line…', 'Remove from order']);
      expect(await menuOf(2)).toEqual(['Defects…']);
      expect(await menuOf(3)).toEqual(['Defects…']);
    });

    it('takes no print out of the order without an archive right', async () => {
      auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust']);
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue(three as never);
      render(<OrderPrints order={lineOrder([1, 2, 3])} canEdit />);
      await screen.findByText('mine.3mf');
      expect(await menuOf(1)).toEqual(['Defects…']);
    });

    // WS-13 E13 O21: the filing right alone files and unfiles ANY print, an ownerless one too —
    // without the right to change orders, so with no defects entry.
    it('the filing clerk files and unfiles every print, and records no defects', async () => {
      auth.granted = new Set(['orders:read', 'orders:file_prints']);
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue(three as never);
      render(<OrderPrints order={lineOrder([1, 2, 3])} canEdit={false} />);
      await screen.findByText('mine.3mf');
      expect(await menuOf(2)).toEqual(['File under line…', 'Remove from order']);
      expect(await menuOf(3)).toEqual(['File under line…', 'Remove from order']);
    });

    it('offers no menu on a print it may not move that has no defects to record', async () => {
      auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust', 'archives:update_own']);
      vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
        { id: 2, filename: 'theirs.3mf', status: 'printing', project_line_id: 10, created_by_id: 8 },
      ] as never);
      render(<OrderPrints order={lineOrder([2])} canEdit />);
      await screen.findByText('theirs.3mf');
      expect(screen.queryByTestId('print-menu-2')).not.toBeInTheDocument();
    });
  });

  it('asks before a print leaves the order, naming it and what happens to it', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1]) as never);
    const remove = vi.spyOn(api, 'removeArchivesFromProject').mockResolvedValue({} as never);
    render(<OrderPrints order={lineOrder([1])} canEdit />);
    await screen.findByTestId('prints-line-10');

    expect(screen.getByTestId('print-menu-1')).toHaveAttribute('aria-haspopup', 'menu');
    fireEvent.click(within(await openMenu(1)).getByRole('menuitem', { name: /remove from/i }));
    const ask = await screen.findByRole('dialog', { name: 'Remove the print «p1.3mf» from the order?' });
    expect(ask).toHaveTextContent(
      'The print stays in the archive. Its usable parts become free stock. A print whose output has already been received cannot be removed.',
    );
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(within(ask).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(1, [1]));
  });

  it('keeps a refused removal in its dialog, in the server’s words (E13 E02)', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1]) as never);
    vi.spyOn(api, 'removeArchivesFromProject').mockRejectedValue(
      new Error('These prints went onto the shelf for the order — they cannot leave it'),
    );
    render(<OrderPrints order={lineOrder([1])} canEdit />);
    await screen.findByTestId('prints-line-10');
    fireEvent.click(within(await openMenu(1)).getByRole('menuitem', { name: /remove from/i }));
    const ask = await screen.findByRole('dialog', { name: 'Remove the print «p1.3mf» from the order?' });
    fireEvent.click(within(ask).getByRole('button', { name: 'Remove' }));
    expect(await within(ask).findByRole('alert')).toHaveTextContent(
      'These prints went onto the shelf for the order — they cannot leave it',
    );
  });

  it('will not unlink the same print twice while the first request is in flight', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1]) as never);
    const remove = vi.spyOn(api, 'removeArchivesFromProject').mockReturnValue(new Promise(() => {}) as never);
    render(<OrderPrints order={lineOrder([1])} canEdit />);
    await screen.findByTestId('prints-line-10');
    fireEvent.click(within(await openMenu(1)).getByRole('menuitem', { name: /remove from/i }));
    const ask = await screen.findByRole('dialog', { name: 'Remove the print «p1.3mf» from the order?' });
    const confirm = within(ask).getByRole('button', { name: 'Remove' });
    fireEvent.click(confirm);
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1));
    fireEvent.click(confirm);
    expect(remove).toHaveBeenCalledTimes(1);
  });

  it('starts a different order at the first page rather than at the cap bought on the last one', async () => {
    const page = rows(Array.from({ length: 500 }, (_, i) => i + 1));
    const get = vi.spyOn(api, 'getProjectArchives').mockResolvedValue(page as never);
    const order = (id: number) =>
      ({ id, other_archive_ids: [], lines: [{ id: 10, product_name: 'F', quantity: 1, archive_ids: [] }] }) as unknown as Order;

    const { rerender } = render(<OrderPrints order={order(1)} canEdit={false} />);
    fireEvent.click(await screen.findByTestId('prints-load-older'));
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(20));

    get.mockClear();
    rerender(<OrderPrints order={order(2)} canEdit={false} />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    await waitFor(() => expect(get.mock.calls.length).toBe(20));
    expect(get.mock.calls.every((call) => call[0] === 2)).toBe(true);
  });

  it('shows how many came out bad and records defects in the framed dialog', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: 10, quantity: 6, defective_count: 2, plate_index: 1 },
    ] as never);
    vi.spyOn(api, 'getOrderPrintParts').mockResolvedValue({
      archive_id: 1,
      quantity: 6,
      defective_count: 2,
      parts: [
        { id: 11, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 },
        { id: 12, name: 'base', name_key: 'base', quantity: 4, defective: 2 },
      ],
    });
    const record = vi.spyOn(api, 'recordOrderPrintDefects').mockResolvedValue({
      archive_id: 1, quantity: 6, defective_count: 3, parts: [],
    });

    render(<OrderPrints order={lineOrder([1])} canEdit />);

    expect((await screen.findByTestId('print-defects-1')).textContent).toContain('2 defective');
    fireEvent.click(within(await openMenu(1)).getByText('Defects…'));
    const dialog = await screen.findByRole('dialog', { name: 'Defects in this print' });
    expect(dialog).toHaveTextContent('a.3mf · plate 1 · 6 pcs');

    const lid = (await screen.findByTestId('part-defective-11')) as HTMLInputElement;
    fireEvent.change(lid, { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('print-defects-save'));

    await waitFor(() =>
      expect(record).toHaveBeenCalledWith(1, 1, { parts: [{ id: 11, defective: 1 }, { id: 12, defective: 2 }] }),
    );
    await waitFor(() => expect(api.getProjectArchives).toHaveBeenCalledTimes(2));
  });

  it('records a flat count for a print without part rows', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 2, filename: 'b.3mf', status: 'completed', project_line_id: 10, quantity: 3, defective_count: 0 },
    ] as never);
    vi.spyOn(api, 'getOrderPrintParts').mockResolvedValue({ archive_id: 2, quantity: 3, defective_count: 0, parts: [] });
    const record = vi.spyOn(api, 'recordOrderPrintDefects').mockResolvedValue({
      archive_id: 2, quantity: 3, defective_count: 1, parts: [],
    });

    render(<OrderPrints order={lineOrder([2])} canEdit />);
    fireEvent.click(within(await openMenu(2)).getByText('Defects…'));
    fireEvent.change(await screen.findByTestId('defective-count-input'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('print-defects-save'));

    await waitFor(() => expect(record).toHaveBeenCalledWith(1, 2, { defective_count: 1 }));
  });

  it('a failed parts fetch says so and offers a retry instead of hanging on Loading', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 3, filename: 'c.3mf', status: 'completed', project_line_id: 10, quantity: 2, defective_count: 0 },
    ] as never);
    const parts = vi
      .spyOn(api, 'getOrderPrintParts')
      .mockRejectedValueOnce(new Error('Print not found in this order'))
      .mockResolvedValue({
        archive_id: 3,
        quantity: 2,
        defective_count: 0,
        parts: [{ id: 31, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 }],
      });

    render(<OrderPrints order={lineOrder([3])} canEdit />);
    fireEvent.click(within(await openMenu(3)).getByText('Defects…'));

    expect(await screen.findByText('Print not found in this order')).toBeInTheDocument();
    expect(screen.getByTestId('print-defects-save')).toBeDisabled();
    expect(screen.getByText('Cancel')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Retry'));

    await waitFor(() => expect(parts.mock.calls.length).toBeGreaterThan(1));
    expect(await screen.findByTestId('part-defective-31')).toBeInTheDocument();
    expect(screen.getByTestId('print-defects-save')).not.toBeDisabled();
  });

  it('offers no menu at all without the permission', async () => {
    // A reader of the order: neither the order's right nor the filing right (WS-13 E13 O21).
    auth.granted = new Set(['orders:read', 'archives:update_all']);
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(rows([1]) as never);
    render(<OrderPrints order={lineOrder([1])} canEdit={false} />);
    await screen.findByTestId('prints-line-10');
    expect(screen.queryByTestId('print-menu-1')).not.toBeInTheDocument();
  });
});
