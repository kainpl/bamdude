/**
 * The Issues section of a printer's queue card: the «Delete all» button
 * counts the failed + cancelled rows the user may delete (never skipped),
 * asks first — with what it froze at the click — and sends exactly those ids
 * to one bulk-delete call.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent, act } from '@testing-library/react';
import { focusManager } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { QueueCard } from '../../components/QueueCard';
import { useAuth } from '../../contexts/AuthContext';
import type { PrinterQueue } from '../../api/client';

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
  server.use(
    http.get('/api/v1/queue/', ({ request }) => {
      const status = new URL(request.url).searchParams.get('status') ?? '';
      return HttpResponse.json(byStatus[status] ?? []);
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

  it('counts failed and cancelled but not skipped, asks, then sends exactly those ids', async () => {
    renderCard();

    const button = await screen.findByRole('button', { name: 'Delete all (3)' });
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

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all (1)' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('failed: 0, cancelled: 1?');
    expect(dialog).not.toHaveTextContent(GATE);
  });

  it('does not open or close the section', async () => {
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all (3)' }));

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
    fireEvent.click(await screen.findByRole('button', { name: 'Delete all (1)' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('failed: 1, cancelled: 0?');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(posted).toEqual([[11]]));
  });

  it('confirms what it showed, even if the list moved while the dialog was open', async () => {
    let failedServed = 0;
    let failed = [row(1, 'failed'), row(2, 'failed')];
    server.use(
      http.get('/api/v1/queue/', ({ request }) => {
        const status = new URL(request.url).searchParams.get('status') ?? '';
        if (status === 'failed') {
          failedServed += 1;
          return HttpResponse.json(failed);
        }
        return HttpResponse.json(status === 'cancelled' ? [row(3, 'cancelled')] : []);
      }),
    );
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all (3)' }));
    await screen.findByRole('dialog');

    // A new failure lands and the lists refetch behind the open dialog.
    failed = [...failed, row(5, 'failed')];
    const before = failedServed;
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(failedServed).toBeGreaterThan(before));

    expect(screen.getByRole('dialog')).toHaveTextContent('failed: 2, cancelled: 1?');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(posted).toEqual([[1, 2, 3]]));
  });

  it('says how many were left in place', async () => {
    posted = answerBulkDelete(() => ({ deleted_count: 2, skipped_count: 1 }));
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all (3)' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('2 removed')).toBeInTheDocument();
    expect(await screen.findByText('1 left in place (changed or removed meanwhile)')).toBeInTheDocument();
  });

  it('says nothing was removed instead of "0 removed"', async () => {
    posted = answerBulkDelete(() => ({ deleted_count: 0, skipped_count: 3 }));
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all (3)' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('Nothing removed — the jobs changed or were removed meanwhile')).toBeInTheDocument();
    expect(screen.queryByText('0 removed')).toBeNull();
  });

  it('closes the dialog and shows the refusal when the request fails', async () => {
    posted = answerBulkDelete(() => HttpResponse.json({ detail: 'The queue is busy' }, { status: 500 }));
    renderCard();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all (3)' }));
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
