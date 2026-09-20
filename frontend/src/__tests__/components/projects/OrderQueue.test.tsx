/**
 * The order's queue panel reads the SHARED farm-wide queue.
 *
 * It used to ask the same two questions under keys of its own — `['queue',
 * 'printing']` and `['queue', 'pending']` — which no other screen used, so
 * TanStack could not tell they were the queue page's `['queue', 'all', …]`
 * question. The same rows were fetched twice, on two timers this component
 * owned, and the two screens could disagree about what was on a printer.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { OrderQueue } from '../../../components/projects/OrderQueue';
import { strayZeroTextNodes } from '../../domHelpers';

const order = {
  id: 1,
  name: 'Ten flasks',
  status: 'active',
  lines: [{ id: 10, product_name: 'Flask' }],
};

const pending = [
  { id: 7, project_id: 1, project_line_id: 10, archive_id: null, archive_thumbnail: null, archive_name: 'Body', library_file_id: null, library_file_thumbnail: null, library_file_name: null, printer_id: 3, printer_name: 'P1S' },
];

function mountWithClient(client: QueryClient) {
  return render(
    <QueryClientProvider client={client}>
      <OrderQueue orderId={1} />
    </QueryClientProvider>,
  );
}

describe('OrderQueue', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);
    vi.spyOn(api, 'getSettings').mockResolvedValue({ time_format: 'system' } as never);
  });

  it('asks the queue page\'s own two questions, under the queue page\'s own keys', async () => {
    const getQueue = vi.spyOn(api, 'getQueue').mockResolvedValue([] as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });

    mountWithClient(client);

    await waitFor(() => expect(getQueue).toHaveBeenCalledTimes(2));
    // The exact call shape the queue page uses — no printer, one status each.
    expect(getQueue).toHaveBeenCalledWith(undefined, 'pending');
    expect(getQueue).toHaveBeenCalledWith(undefined, 'printing');

    const keys = client
      .getQueryCache()
      .findAll({ queryKey: ['queue'] })
      .map((q) => q.queryKey);
    expect(keys).toEqual(
      expect.arrayContaining([
        ['queue', 'all', 'pending'],
        ['queue', 'all', 'printing'],
      ]),
    );
    expect(keys).toHaveLength(2);
  });

  it('renders the shared data already in the cache without fetching its own copy', async () => {
    // The panel has no poll of its own: whatever filled `['queue', 'all', …]`
    // — the queue page, a websocket invalidation, the shared interval — is
    // what it draws. A private key could not have been served from here at all.
    const getQueue = vi.spyOn(api, 'getQueue').mockResolvedValue([] as never);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 60_000 } },
    });
    client.setQueryData(['queue', 'all', 'pending'], pending);
    client.setQueryData(['queue', 'all', 'printing'], []);

    mountWithClient(client);

    expect(await screen.findByText('Body')).toBeInTheDocument();
    expect(getQueue).not.toHaveBeenCalled();
    // ⚠️ No bare `0` in the rendered list. `count && <X/>` renders the NUMBER
    // when the count is zero, and a queue is exactly where an empty count is
    // normal — the digit then sits in the layout looking like a figure.
    // `queryAllByText` cannot see it: the zero is a text node among an
    // element's other children (`__tests__/domHelpers`).
    expect(strayZeroTextNodes()).toHaveLength(0);
  });
  it('asks for the order through the same options the page does, meta included', async () => {
    // ⚠️ A query has ONE `meta`, set by whichever observer mounted last. This
    // panel watches `['project', id]` too; when it declared its own options
    // without `meta` it wiped the page's `refreshToast` flag, and a failed
    // background refetch went unreported on exactly the page the flag was
    // added for. Measured, not assumed — hence `useOrderDetail`, which both
    // read through.
    vi.spyOn(api, 'getQueue').mockResolvedValue([] as never);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });

    mountWithClient(client);

    await waitFor(() =>
      expect(client.getQueryCache().find({ queryKey: ['project', 1] })?.meta).toEqual({ refreshToast: true }),
    );
  });
});
