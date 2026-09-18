import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { InboxTab } from '../../../components/notifications/InboxTab';

const items = [
  {
    id: 2, event_type: 'print_failed', severity: 'error', group: 'print', title: 'Print failed on P1',
    message: 'Benchy failed', printer_id: 1, printer_name: 'P1', extra_data: null,
    created_at: new Date().toISOString(), read_at: null,
  },
  {
    id: 1, event_type: 'print_complete', severity: 'info', group: 'print', title: 'Print done',
    message: 'Benchy done', printer_id: 1, printer_name: 'P1', extra_data: null,
    created_at: new Date().toISOString(), read_at: new Date().toISOString(),
  },
];

/** One page in the shape the endpoint returns — meta named as Archives names it. */
function pageOf(items: unknown[], unread_count: number, over: Record<string, unknown> = {}) {
  return {
    items,
    unread_count,
    total: items.length,
    current_page: 1,
    per_page: 24,
    last_page: 1,
    ...over,
  };
}

describe('InboxTab', () => {
  const calls: { read: number[]; readAll: URL[]; clear: URL[] } = { read: [], readAll: [], clear: [] };

  beforeEach(() => {
    calls.read = [];
    calls.readAll = [];
    calls.clear = [];
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'P1' }])),
      http.get('/api/v1/inbox/', ({ request }) => {
        const url = new URL(request.url);
        const filtered = url.searchParams.get('severity') === 'error' ? [items[0]] : items;
        return HttpResponse.json(pageOf(filtered, 1));
      }),
      http.post('/api/v1/inbox/:id/read', ({ params }) => {
        calls.read.push(Number(params.id));
        return HttpResponse.json({ ...items[0], read_at: new Date().toISOString() });
      }),
      http.post('/api/v1/inbox/read-all', ({ request }) => {
        calls.readAll.push(new URL(request.url));
        return HttpResponse.json({ updated: 1 });
      }),
      http.delete('/api/v1/inbox/', ({ request }) => {
        calls.clear.push(new URL(request.url));
        return HttpResponse.json({ deleted: 1 });
      }),
    );
  });

  it('lists items newest first with one unread dot', async () => {
    render(<InboxTab />);
    const rows = await screen.findAllByRole('listitem');
    expect(within(rows[0]).getByText('Print failed on P1')).toBeInTheDocument();
    expect(screen.getAllByTestId('unread-dot')).toHaveLength(1);
  });

  it('marks a row read on click', async () => {
    render(<InboxTab />);
    const row = (await screen.findAllByRole('listitem'))[0];
    await userEvent.click(within(row).getByText('Print failed on P1'));
    await waitFor(() => expect(calls.read).toEqual([2]));
  });

  it('read-all carries the active severity filter', async () => {
    render(<InboxTab />);
    await screen.findAllByRole('listitem');
    await userEvent.selectOptions(screen.getByLabelText('Level'), 'error');
    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(1));
    await userEvent.click(screen.getByRole('button', { name: 'Mark all read' }));
    await waitFor(() => expect(calls.readAll).toHaveLength(1));
    expect(calls.readAll[0].searchParams.get('severity')).toBe('error');
  });

  // The destructive path: Clear deletes exactly what the filters are showing,
  // so the filter must reach the DELETE and nothing may go before the confirm.
  it('clear asks first, then deletes under the active filter', async () => {
    render(<InboxTab />);
    await screen.findAllByRole('listitem');
    await userEvent.selectOptions(screen.getByLabelText('Level'), 'error');
    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(1));

    await userEvent.click(screen.getByRole('button', { name: 'Clear' }));
    const dialog = await screen.findByRole('dialog');
    expect(calls.clear).toHaveLength(0);

    await userEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(calls.clear).toHaveLength(1));
    expect(calls.clear[0].searchParams.get('severity')).toBe('error');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
  it('does not offer mark-all-read when every row on screen is already read', async () => {
    // The server's unread_count is the WHOLE inbox — it feeds the sidebar badge —
    // while mark-all-read acts on the filtered set. Filter to rows that are all
    // read and the button must go quiet, or it fires a no-op that reports zero.
    server.use(
      http.get('/api/v1/inbox/', () =>
        HttpResponse.json(pageOf([items[1]], 5)),
      ),
    );
    render(<InboxTab />);
    await screen.findByText('Print done');
    expect(screen.getByRole('button', { name: 'Mark all read' })).toBeDisabled();
  });

  it('pages with the same bar the rest of the app uses, and asks for the size chosen', async () => {
    // The inbox used to grow by a Load-more button while every other table in
    // the app had numbered pages and a size selector; this pins that it now
    // asks the server for a page rather than appending to one long list.
    const asked: string[] = [];
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/inbox/', ({ request }) => {
        asked.push(new URL(request.url).search);
        return HttpResponse.json(pageOf([items[0]], 1, { total: 60, last_page: 3 }));
      }),
    );

    render(<InboxTab />);

    await screen.findByText('Print failed on P1');
    await userEvent.click(await screen.findByRole('button', { name: /next page/i }));

    await waitFor(() => expect(asked.some((q) => q.includes('page=2'))).toBe(true));
  });

});
