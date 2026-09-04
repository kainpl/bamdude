/**
 * Grouping comes from the RESPONSE, not from the archives.
 *
 * `lines[].archive_ids` is not a partition — one print can count against two
 * lines at once (a plate that carries parts of both), so the same archive is
 * expected under two headings and an id-set walk over the archives would show
 * it under neither. Whatever no line claimed lands under "other prints" from
 * `other_archive_ids`; the leftover group exists only as a defensive net.
 *
 * The other half of this file is the READ: the order names every archive it
 * counts, so the page keeps asking for pages until it holds them all. Reading
 * one page and captioning the shortfall was honest and useless — the figures
 * counted prints the grid could not show.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order } from '../../../api/client';
import { OrderPrints } from '../../../components/projects/OrderPrints';
import { strayZeroTextNodes } from '../../domHelpers';

/** `id` → a minimal archive row, the shape `getProjectArchives` answers with. */
function rows(ids: number[], lineId: number | null = 10) {
  return ids.map((id) => ({
    id,
    filename: `p${id}.3mf`,
    status: 'completed',
    project_line_id: lineId,
  }));
}

describe('OrderPrints', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
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
        { id: 10, product_name: 'Flask', quantity: 2, archive_ids: [1, 2] },
        { id: 11, product_name: 'Lid', quantity: 1, archive_ids: [2] },
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

  it('says which of two prints under the same line was filed by hand', async () => {
    // The badge reads `archive.project_line_id`, NOT the group the card is
    // drawn in. Both of these hang under line 10 — one because an operator
    // filed it there, one because the server's accounting attributed it — and
    // a badge derived from the grouping would call both of them "Filed",
    // erasing the only signal that says whose decision it was.
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'by-hand.3mf', status: 'completed', project_line_id: 10 },
      { id: 2, filename: 'by-the-server.3mf', status: 'completed', project_line_id: null },
    ] as never);

    const order = {
      id: 1,
      other_archive_ids: [],
      lines: [{ id: 10, product_name: 'Flask', quantity: 2, archive_ids: [1, 2] }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    const group = await screen.findByTestId('prints-line-10');
    const card = (name: string) => screen.getByText(name).closest('div.relative') as HTMLElement;

    expect(within(card('by-hand.3mf')).getByText('Filed')).toBeInTheDocument();
    expect(within(card('by-the-server.3mf')).getByText('Attributed')).toBeInTheDocument();
    // The filed one also names the line it was filed under; the attributed one
    // has nothing to name, because nobody named it.
    expect(within(card('by-hand.3mf')).getByText('Filed')).toHaveAttribute('title', 'Flask');
    expect(within(card('by-the-server.3mf')).getByText('Attributed')).not.toHaveAttribute('title');
    // Both are in the same group — the badge is the only thing that differs.
    expect(group.textContent).toContain('by-hand.3mf');
    expect(group.textContent).toContain('by-the-server.3mf');
  });

  it('shows an archive no group claimed rather than dropping it', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 7, filename: 'stray.3mf', status: 'completed', project_line_id: null },
    ] as never);

    const order = {
      id: 1,
      other_archive_ids: [],
      lines: [{ id: 10, product_name: 'Flask', quantity: 1, archive_ids: [] }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    expect((await screen.findByTestId('prints-unlisted')).textContent).toContain('stray.3mf');
  });

  it('links a print to its library file when there is one, and to the name alone otherwise', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'linked.3mf', status: 'completed', project_line_id: 10, library_file_id: 42 },
      { id: 2, filename: 'external print.3mf', status: 'completed', project_line_id: 10, library_file_id: null },
    ] as never);

    const order = {
      id: 1,
      other_archive_ids: [],
      lines: [{ id: 10, product_name: 'Flask', quantity: 2, archive_ids: [1, 2] }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    const group = await screen.findByTestId('prints-line-10');
    const hrefs = Array.from(group.querySelectorAll('a')).map((a) => a.getAttribute('href'));
    // ``file`` is the only param that FILTERS; ``fileName`` just labels the chip.
    expect(hrefs).toContain('/archives?file=42&fileName=linked.3mf');
    // No library file id means nothing to filter ON, so the link does not
    // pretend: a bare `?fileName=` opened the whole archive list wearing this
    // print's name, which reads as a filter that quietly did nothing.
    expect(hrefs).toContain('/archives');
    expect(hrefs).not.toContain('/archives?fileName=external%20print.3mf');
  });

  it('keeps reading pages until every archive the order names is loaded', async () => {
    // A FULL page says nothing about whether it was the last one, so the walk
    // asks again; the short second page ends it. 750 prints, two reads — where
    // the old page read 500 and captioned the missing 250 as "truncated".
    const all = rows(Array.from({ length: 750 }, (_, i) => i + 1));
    const getArchives = vi
      .spyOn(api, 'getProjectArchives')
      .mockImplementation((async (_id: number, limit = 500, offset = 0) =>
        all.slice(offset, offset + limit)) as never);

    const order = {
      id: 1,
      other_archive_ids: [],
      lines: [{ id: 10, product_name: 'Flask', quantity: 750, archive_ids: all.map((a) => a.id) }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    const group = await screen.findByTestId('prints-line-10');
    // The last print of the SECOND page: proof the walk did not stop at 500.
    await waitFor(() => expect(group.textContent).toContain('p750.3mf'));
    expect(getArchives).toHaveBeenCalledTimes(2);
    expect(getArchives).toHaveBeenNthCalledWith(1, 1, 500, 0);
    expect(getArchives).toHaveBeenNthCalledWith(2, 1, 500, 500);
    expect(screen.queryByTestId('prints-load-older')).not.toBeInTheDocument();
  });

  it('stops at its page guard and offers the rest as a button', async () => {
    // The stub answers every offset with the SAME full page, so the walk runs
    // to its twenty-page cap without ten thousand cards in jsdom. Overlapping
    // pages are real — rows arrive mid-walk — which is why the loader keys
    // archives by id and only the 500 distinct ones render.
    const page = rows(Array.from({ length: 500 }, (_, i) => i + 1));
    const getArchives = vi.spyOn(api, 'getProjectArchives').mockResolvedValue(page as never);

    const order = {
      id: 1,
      other_archive_ids: [],
      // 9999 is never answered, so the walk can never satisfy the order and
      // stops only because the guard says so.
      lines: [{ id: 10, product_name: 'Flask', quantity: 600, archive_ids: [...page.map((a) => a.id), 9999] }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    const button = await screen.findByTestId('prints-load-older');
    expect(getArchives).toHaveBeenCalledTimes(20);
    expect(getArchives).not.toHaveBeenCalledWith(1, 500, 20 * 500);

    fireEvent.click(button);

    // One more page than last time — the button buys pages, it does not restate
    // the shortfall.
    await waitFor(() => expect(getArchives).toHaveBeenCalledWith(1, 500, 20 * 500));
  });

  it('keeps the button away while one page holds everything', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: 10 },
    ] as never);

    const order = {
      id: 1,
      other_archive_ids: [],
      lines: [{ id: 10, product_name: 'Flask', quantity: 1, archive_ids: [1] }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    expect(await screen.findByTestId('prints-line-10')).toBeInTheDocument();
    expect(screen.queryByTestId('prints-load-older')).not.toBeInTheDocument();
    // ⚠️ No bare `0` anywhere in the grid. `count && <X/>` renders the number
    // when the count is zero, and a list is exactly where an empty one is
    // normal — the digit then sits in the layout looking like data.
    expect(strayZeroTextNodes(screen.getByTestId('prints-line-10'))).toHaveLength(0);
  });

  // ⚠️ The card's actions live in the SHARED `CardActionMenu` now, not in a
  // hand-rolled panel: `role="menu"`, roving arrow keys, Escape, and one z-stack
  // decided in one place. These two tests are what says the actions still reach
  // the API through it.
  it('files a print under a line from its menu', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: null },
    ] as never);
    const update = vi.spyOn(api, 'updateArchive').mockResolvedValue({} as never);
    const order = {
      id: 1,
      other_archive_ids: [1],
      lines: [{ id: 10, product_name: 'Flask', quantity: 1, archive_ids: [] }],
    } as unknown as Order;
    vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);

    render(<OrderPrints order={order} canEdit />);
    await screen.findByTestId('prints-other');

    fireEvent.click(screen.getByTestId('print-menu-1'));
    fireEvent.click(await screen.findByRole('menuitem', { name: /file under/i }));
    // The picker reads the order through `useOrderDetail`; wait for its lines.
    await screen.findByRole('option', { name: /Flask/ });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '10' } });

    // ⚠️ `project_id` travels with the line — a bare line change on an archive
    // whose order is being re-stated is a 400.
    await waitFor(() => expect(update).toHaveBeenCalledWith(1, { project_id: 1, project_line_id: 10 }));
  });

  it('takes a print off the order from its menu', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: 10 },
    ] as never);
    const remove = vi.spyOn(api, 'removeArchivesFromProject').mockResolvedValue({} as never);
    const order = {
      id: 1,
      other_archive_ids: [],
      lines: [{ id: 10, product_name: 'Flask', quantity: 1, archive_ids: [1] }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);
    await screen.findByTestId('prints-line-10');

    const trigger = screen.getByTestId('print-menu-1');
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole('menuitem', { name: /remove from/i }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith(1, [1]));
  });

  it('will not unlink the same print twice while the first request is in flight', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: 10 },
    ] as never);
    // Never settles, so the menu item stays in its pending state for the assertion.
    const remove = vi.spyOn(api, 'removeArchivesFromProject').mockReturnValue(new Promise(() => {}) as never);
    const order = {
      id: 1,
      other_archive_ids: [],
      lines: [{ id: 10, product_name: 'Flask', quantity: 1, archive_ids: [1] }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);
    await screen.findByTestId('prints-line-10');
    fireEvent.click(screen.getByTestId('print-menu-1'));

    const item = await screen.findByRole('menuitem', { name: /remove from/i });
    fireEvent.click(item);
    await waitFor(() => expect(item).toBeDisabled());

    // The second click is the one the hand-rolled button used to refuse and the
    // ported menu item did not.
    fireEvent.click(item);
    expect(remove).toHaveBeenCalledTimes(1);
  });

  it('starts a different order at the first page rather than at the cap bought on the last one', async () => {
    // A full page, so the walk always reports `truncated` and the button shows.
    const page = rows(Array.from({ length: 500 }, (_, i) => i + 1));
    const get = vi.spyOn(api, 'getProjectArchives').mockResolvedValue(page as never);
    const order = (id: number) =>
      ({ id, other_archive_ids: [], lines: [{ id: 10, product_name: 'F', quantity: 1, archive_ids: [] }] }) as
        unknown as Order;

    const { rerender } = render(<OrderPrints order={order(1)} canEdit={false} />);
    fireEvent.click(await screen.findByTestId('prints-load-older'));
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(20));

    // ⚠️ The cap is per order. Reset in an effect it would survive one render of
    // the NEW order — long enough to issue a 21-page walk of somebody else's
    // history before the reset landed.
    get.mockClear();
    rerender(<OrderPrints order={order(2)} canEdit={false} />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    await waitFor(() => expect(get.mock.calls.length).toBe(20));
    expect(get.mock.calls.every((call) => call[0] === 2)).toBe(true);
  });

  it('offers no menu at all without the permission', async () => {
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue([
      { id: 1, filename: 'a.3mf', status: 'completed', project_line_id: 10 },
    ] as never);
    const order = {
      id: 1,
      other_archive_ids: [],
      lines: [{ id: 10, product_name: 'Flask', quantity: 1, archive_ids: [1] }],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit={false} />);

    await screen.findByTestId('prints-line-10');
    expect(screen.queryByTestId('print-menu-1')).not.toBeInTheDocument();
  });
});
