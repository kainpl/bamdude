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
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order } from '../../../api/client';
import { OrderPrints } from '../../../components/projects/OrderPrints';

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
  });
});
