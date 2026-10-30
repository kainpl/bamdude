import { useState } from 'react';
/**
 * The Issues section of a printer's queue card: the «Delete all» button
 * counts the failed + cancelled rows the user may delete (never skipped),
 * asks first — with what it froze at the click — and sends exactly those ids
 * to one bulk-delete call.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { QueueCard } from '../../components/QueueCard';
import { useAuth } from '../../contexts/AuthContext';
import { api, type PrinterQueue } from '../../api/client';

const queue: PrinterQueue = {
  id: 7,
  printer_id: 7,
  printer_name: 'A1-07',
  printer_model: 'A1',
  status: 'idle',
  is_paused: false,
  auto_distribute_eligible: true,
  last_activity_at: null,
  current_item_id: null,
  pending_count: 0,
  completed_count: 145,
  failed_count: 8,
  cancelled_count: 2,
  skipped_count: 1,
  total_count: 156,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-22T00:00:00Z',
};

const row = (id: number, status: string, createdById: number | null = 1) => ({
  id,
  queue_id: 7,
  printer_id: 7,
  archive_id: id,
  position: id,
  status,
  archive_name: 'W49699__upd_plate_1',
  printer_name: 'A1-07',
  source_storage: 'ready',
  created_by_id: createdById,
});

const GATE = 'Jobs set to wait for a successful previous print will no longer be held back by these failures.';

/** Renders the signed-in username, so a test can wait until the permissions it asserts on are loaded. */
function WhoAmI() {
  const { user } = useAuth();
  return <span>{user ? `user:${user.username}` : 'user:none'}</span>;
}

function renderCard() {
  return render(
    <>
      <QueueCard queue={queue} onEditItem={vi.fn()} />
      <WhoAmI />
    </>,
  );
}

function signInAs(username: string, id: number, permissions: string[]) {
  server.use(
    http.get('/api/v1/auth/me', () =>
      HttpResponse.json({
        id,
        username,
        role: 'user',
        is_active: true,
        is_admin: false,
        groups: [{ id: 2, name: 'Operators' }],
        permissions,
        created_at: '2026-01-01T00:00:00Z',
      }),
    ),
  );
}

function mockQueue(byStatus: Record<string, unknown[]>) {
  const issues = Object.values(byStatus).flat() as Array<{ id: number; status: string }>;
  server.use(
    http.get('/api/v1/queue/', ({ request }) => {
      const status = new URL(request.url).searchParams.get('status') ?? '';
      return HttpResponse.json(byStatus[status] ?? []);
    }),
    http.get('/api/v1/queue/summary', () => HttpResponse.json({
      pending_count: 0,
      groups: [{
        queue_id: 7, printer_id: 7, pending_count: 0,
        failed_count: byStatus.failed?.length ?? 0,
        cancelled_count: byStatus.cancelled?.length ?? 0,
        skipped_count: byStatus.skipped?.length ?? 0,
      }],
    })),
    http.get('/api/v1/queue/issues', ({ request }) => {
      const params = new URL(request.url).searchParams;
      const cursor = Number(params.get('cursor') ?? 0);
      const limit = Number(params.get('limit') ?? 50);
      const selected = issues.filter(item => item.id > cursor).sort((a, b) => a.id - b.id);
      return HttpResponse.json({ items: selected.slice(0, limit), next_cursor: selected.length > limit ? selected[limit - 1].id : null });
    }),
    http.get('/api/v1/printers/7/status', () => HttpResponse.json({ state: 'IDLE' })),
  );
}

function answerBulkDelete(answer: (ids: number[]) => Response | { deleted_count: number; skipped_count: number }) {
  const posted: number[][] = [];
  server.use(
    http.post('/api/v1/queue/bulk-delete', async ({ request }) => {
      const body = (await request.json()) as { item_ids: number[] };
      posted.push(body.item_ids);
      const out = answer(body.item_ids);
      return out instanceof Response ? out : HttpResponse.json({ ...out, message: 'ok' });
    }),
  );
  return posted;
}

describe('QueueCard collapsed copies', () => {
  it('keeps editing and cancellation available in the visible action menu', async () => {
    const edit = vi.fn();
    const cancel = vi.spyOn(api, 'cancelQueueItem').mockResolvedValue({ success: true } as never);
    mockQueue({ pending: [row(1, 'pending')] });
    render(<QueueCard queue={queue} onEditItem={edit} />);
    const more = await screen.findByTitle('More');
    fireEvent.click(more);
    fireEvent.click(screen.getByRole('button', { name: 'Edit item' }));
    expect(edit).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }));
    fireEvent.click(more);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel item' }));
    await waitFor(() => expect(cancel).toHaveBeenCalledWith(1));
    cancel.mockRestore();
  });

  it.each([[[120, 120], '2m'], [[120, 180], '—'], [[120, null], '—']] as const)(
    'shows a per-copy estimate for %s and only transfers expanded copies', async (times, label) => {
      localStorage.setItem('queueCollapsedBatches', JSON.stringify(['estimate-batch']));
      mockQueue({ pending: times.map((seconds, index) => ({ ...row(index + 1, 'pending'),
        batch_id: 'estimate-batch', print_time_seconds: seconds })) });
      renderCard();
      const duration = await screen.findByText(label);
      expect(duration.title).toMatch(label === '—' ? /differ|unknown/i : /per print/i);
      expect(screen.queryByRole('button', { name: 'Move to auto queue' })).not.toBeInTheDocument();
      fireEvent.click(screen.getByTitle('Expand batch'));
      expect(await screen.findAllByRole('button', { name: 'Move to auto queue' })).toHaveLength(2);
      localStorage.removeItem('queueCollapsedBatches');
    },
  );
});

describe('QueueCard Issues — Delete all', () => {
  let posted: number[][];

  beforeEach(() => {
    mockQueue({
      failed: [row(1, 'failed'), row(2, 'failed')],
      cancelled: [row(3, 'cancelled')],
      skipped: [row(4, 'skipped')],
    });
    posted = answerBulkDelete((ids) => ({ deleted_count: ids.length, skipped_count: 0 }));
  });

  it('keeps issue rows unloaded while closed, then pages the open section', async () => {
    const many = Array.from({ length: 55 }, (_, index) => ({ ...row(index + 1, 'failed'), archive_name: `Issue ${index + 1}` }));
    mockQueue({ failed: many });
    let detailReads = 0;
    server.use(http.get('/api/v1/queue/issues', ({ request }) => {
      detailReads++;
      const params = new URL(request.url).searchParams;
      const cursor = Number(params.get('cursor') ?? 0);
      const limit = Number(params.get('limit') ?? 50);
      const selected = many.filter(item => item.id > cursor);
      return HttpResponse.json({ items: selected.slice(0, limit), next_cursor: selected.length > limit ? selected[limit - 1].id : null });
    }));
    renderCard();
    const header = await screen.findByText('Issues (55)');
    expect(detailReads).toBe(0);
    fireEvent.click(header);
    expect(await screen.findByText('Issue 50')).toBeInTheDocument();
    expect(screen.queryByText('Issue 55')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Load more issues' }));
    expect(await screen.findByText('Issue 55')).toBeInTheDocument();
    expect(detailReads).toBe(2);
  });

  it('loads every page before freezing bulk-delete IDs', async () => {
    mockQueue({ failed: Array.from({ length: 101 }, (_, index) => row(index + 1, 'failed')) });
    renderCard();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('failed: 101, cancelled: 0?');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(posted).toEqual([Array.from({ length: 101 }, (_, index) => index + 1)]));
  });

  it('cannot open confirmation or write after cancelling a slow bulk load', async () => {
    server.use(http.get('/api/v1/queue/issues', async () => {
      await delay(150);
      return HttpResponse.json({ items: [row(1, 'failed')], next_cursor: null });
    }));
    renderCard();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel loading' }));
    await screen.findByRole('button', { name: 'Delete all…' });
    await delay(200);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(posted).toEqual([]);
  });

  it('counts failed and cancelled but not skipped, asks, then sends exactly those ids', async () => {
    renderCard();

    const button = await screen.findByRole('button', { name: 'Delete all…' });
    fireEvent.click(button);

    // Counts stand after labels, never before a noun: two numbers in one
    // sentence cannot both agree with it (uk «1 невдалих»).
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Remove from the queue of A1-07 — failed: 2, cancelled: 1?');
    // Deleting failures is acknowledging them — the dialog says what that releases.
    expect(dialog).toHaveTextContent(GATE);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(posted).toEqual([[1, 2, 3]]));
    expect(await screen.findByText('3 removed')).toBeInTheDocument();
  });

  it('does not mention the previous-success gate when only cancellations go', async () => {
    mockQueue({ cancelled: [row(3, 'cancelled')] });
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('failed: 0, cancelled: 1?');
    expect(dialog).not.toHaveTextContent(GATE);
  });

  it('does not open or close the section', async () => {
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));

    // The rows only render while the section is open; it starts collapsed.
    expect(screen.queryByText('W49699__upd_plate_1')).toBeNull();
  });

  it('counts only the rows this user may delete', async () => {
    // Operators read the whole farm but delete only their own; an external
    // print's row has no creator and needs delete_all.
    signInAs('op', 5, ['queue:read', 'queue:read_all', 'queue:delete_own']);
    mockQueue({
      failed: [row(11, 'failed', 5), row(12, 'failed', 9)],
      cancelled: [row(13, 'cancelled', null)],
    });
    renderCard();

    await screen.findByText('user:op');
    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('failed: 1, cancelled: 0?');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(posted).toEqual([[11]]));
  });

  it('confirms what it showed, even if the list moved while the dialog was open', async () => {
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));
    await screen.findByRole('dialog');

    // A later response differs, but the confirmation retains its frozen IDs.
    mockQueue({ failed: [row(1, 'failed'), row(2, 'failed'), row(5, 'failed')], cancelled: [row(3, 'cancelled')] });

    expect(screen.getByRole('dialog')).toHaveTextContent('failed: 2, cancelled: 1?');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(posted).toEqual([[1, 2, 3]]));
  });

  it('says how many were left in place', async () => {
    posted = answerBulkDelete(() => ({ deleted_count: 2, skipped_count: 1 }));
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('2 removed')).toBeInTheDocument();
    expect(await screen.findByText('1 left in place (changed or removed meanwhile)')).toBeInTheDocument();
  });

  it('says nothing was removed instead of "0 removed"', async () => {
    posted = answerBulkDelete(() => ({ deleted_count: 0, skipped_count: 3 }));
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('Nothing removed — the jobs changed or were removed meanwhile')).toBeInTheDocument();
    expect(screen.queryByText('0 removed')).toBeNull();
  });

  it('closes the dialog and shows the refusal when the request fails', async () => {
    posted = answerBulkDelete(() => HttpResponse.json({ detail: 'The queue is busy' }, { status: 500 }));
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all…' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('The queue is busy')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('the per-row remove says what it does, in its tooltip and its toast', async () => {
    server.use(http.delete('/api/v1/queue/1', () => HttpResponse.json({ message: 'Queue item deleted' })));
    renderCard();
    fireEvent.click(await screen.findByText('Issues (4)'));

    const removeButtons = await screen.findAllByTitle('Remove from queue');
    expect(removeButtons).toHaveLength(4); // failed ×2, cancelled, skipped
    fireEvent.click(removeButtons[0]);

    expect(await screen.findByText('Removed from queue')).toBeInTheDocument();
  });

  it('is absent without a delete permission', async () => {
    signInAs('viewer', 2, ['queue:read']);
    renderCard();

    // Both the viewer's permissions and the issue rows are in before the check.
    await screen.findByText('user:viewer');
    await screen.findByText('Issues (4)');
    expect(screen.queryByRole('button', { name: /Delete all/ })).toBeNull();
  });
});

describe('QueueCard Issues — Retry all failed', () => {
  let requests: { queue_id: number; item_ids: number[] }[];
  beforeEach(() => {
    vi.restoreAllMocks(); requests = [];
    mockQueue({ failed: [row(1, 'failed'), row(2, 'failed')], cancelled: [row(3, 'cancelled')] });
    server.use(http.post('/api/v1/queue/bulk-retry', async ({ request }) => {
      const body = await request.json() as { queue_id: number; item_ids: number[] }; requests.push(body);
      return HttpResponse.json({ retried: body.item_ids, left_in_place: [], not_attempted: [] });
    }));
  });
  async function confirm() {
    fireEvent.click(await screen.findByRole('button', { name: 'Retry all…' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' })); return dialog;
  }
  it('loads all pages then sends one frozen confirmed block', async () => {
    mockQueue({ failed: Array.from({ length: 101 }, (_, index) => row(index + 1, 'failed')), cancelled: [row(102, 'cancelled')] });
    renderCard(); fireEvent.click(await screen.findByRole('button', { name: 'Retry all…' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('101'); expect(dialog).toHaveTextContent('A1-07'); expect(requests).toEqual([]);
    mockQueue({ failed: [row(999, 'failed')] });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('101 returned to the queue')).toBeInTheDocument();
    expect(requests).toEqual([{ queue_id: 7, item_ids: Array.from({ length: 101 }, (_, index) => index + 1) }]);
  });
  it('reports every refusal category and not attempted', async () => {
    mockQueue({ failed: [row(1, 'failed'), row(2, 'failed'), row(5, 'failed'), row(6, 'failed')] });
    server.use(http.post('/api/v1/queue/bulk-retry', () => HttpResponse.json({ retried: [1], left_in_place: [{ id: 2, reason: 'source_unavailable' }, { id: 5, reason: 'not_failed' }], not_attempted: [6] })));
    renderCard(); await confirm(); const toast = await screen.findByText(/Confirmed for 1 of 4 jobs/);
    expect(toast).toHaveTextContent('Saved source unavailable: 1'); expect(toast).toHaveTextContent('No longer failed: 1'); expect(toast).toHaveTextContent('Not attempted: 1');
  });
  it.each([403, 503])('does not replay an unconfirmed result (%s)', async status => {
    let calls = 0;
    server.use(http.post('/api/v1/queue/bulk-retry', () => { calls++; return HttpResponse.json({ detail: 'Retry unavailable' }, { status }); }));
    renderCard(); await confirm(); expect(await screen.findByText(/Retry result could not be confirmed/)).toBeInTheDocument(); expect(calls).toBe(1);
  });
  it('owns the full operation past card unmount', async () => {
    let release = () => {};
    const barrier = new Promise<void>(resolve => { release = resolve; });
    server.use(http.post('/api/v1/queue/bulk-retry', async ({ request }) => {
      requests.push(await request.json() as { queue_id: number; item_ids: number[] }); await barrier;
      return HttpResponse.json({ retried: [1, 2], left_in_place: [], not_attempted: [] });
    }));
    const retry = vi.spyOn(api, 'bulkRetryQueueItems');
    const single = vi.spyOn(api, 'retryQueueItem');
    function ToggleCard() {
      const [visible, setVisible] = useState(true);
      return <><button onClick={() => setVisible(value => !value)}>Toggle card</button>{visible && <QueueCard queue={queue} onEditItem={vi.fn()} />}</>;
    }
    render(<ToggleCard />); const dialog = await confirm();
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(screen.getByRole('button', { name: 'Delete all…' })).toBeDisabled(); expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Toggle card' }));
    fireEvent.click(screen.getByRole('button', { name: 'Toggle card' }));
    expect(await screen.findByRole('button', { name: 'Retry all…' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Toggle card' }));
    release(); await retry.mock.results[0].value;
    expect(await screen.findByText('2 returned to the queue')).toBeInTheDocument();
    expect(requests[0].item_ids).toEqual([1, 2]); expect(single).not.toHaveBeenCalled();
  });
  it('hides bulk retry without update permission', async () => {
    signInAs('viewer', 2, ['queue:read']); renderCard(); await screen.findByText('user:viewer'); expect(screen.queryByRole('button', { name: 'Retry all…' })).toBeNull();
  });
  it('can cancel page loading before any operation', async () => {
    server.use(http.get('/api/v1/queue/issues', async () => { await delay(150); return HttpResponse.json({ items: [row(1, 'failed')], next_cursor: null }); }));
    renderCard(); fireEvent.click(await screen.findByRole('button', { name: 'Retry all…' })); fireEvent.click(await screen.findByRole('button', { name: 'Cancel loading' }));
    await delay(200); expect(requests).toEqual([]); expect(screen.queryByRole('dialog')).toBeNull();
  });
});
