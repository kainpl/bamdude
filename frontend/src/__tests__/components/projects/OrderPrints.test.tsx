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
    expect(hrefs).toContain('/archives?fileName=external%20print.3mf');
  });
});
