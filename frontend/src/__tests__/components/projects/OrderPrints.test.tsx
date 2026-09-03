/**
 * Grouping comes from the RESPONSE, not from the archives.
 *
 * `lines[].archive_ids` is not a partition — one print can count against two
 * lines at once (a plate that carries parts of both), so the same archive is
 * expected under two headings and an id-set walk over the archives would show
 * it under neither. Whatever no line claimed lands under "other prints" from
 * `other_archive_ids`; the leftover group exists only as a defensive net.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order } from '../../../api/client';
import { OrderPrints } from '../../../components/projects/OrderPrints';

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

  it('says so when the response filled a whole page and older prints were left behind', async () => {
    // 500 back means 500 was the LIMIT, not the count. `pick()` drops every id
    // it cannot resolve in silence, while the figures above still count them —
    // so without the notice the page shows fewer prints than it claims.
    const loaded = Array.from({ length: 500 }, (_, i) => ({
      id: i + 1,
      filename: `p${i + 1}.3mf`,
      status: 'completed',
      project_line_id: 10,
    }));
    vi.spyOn(api, 'getProjectArchives').mockResolvedValue(loaded as never);

    const order = {
      id: 1,
      other_archive_ids: [900],
      lines: [
        { id: 10, product_name: 'Flask', quantity: 600, archive_ids: [...loaded.map((a) => a.id), 601, 602] },
      ],
    } as unknown as Order;

    render(<OrderPrints order={order} canEdit />);

    const notice = await screen.findByTestId('prints-truncated');
    expect(notice.textContent).toContain('500');
    // 500 loaded + ids 601 and 602 + the one under other prints.
    expect(notice.textContent).toContain('503');
  });

  it('keeps the notice away while one page holds everything', async () => {
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
    expect(screen.queryByTestId('prints-truncated')).not.toBeInTheDocument();
  });
});
