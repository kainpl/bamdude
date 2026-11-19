/**
 * A batch action names the printer queue the operator acts in (G03 D1).
 *
 * AutoQueue promotion copies the router row's `batch_id` onto every printer row it
 * creates, so the copies of one AutoQueue batch sit in several printers' queues. The
 * batch routes used to act farm-wide (cancel / skip) or on the queue of whichever copy
 * sat lowest (reorder / clone); they now take `?queue_id=`, and refuse a batch that
 * spans queues without it. Every door of the UI sends the queue of the card — or of
 * the timeline lane — the action was started from.
 *
 * Also here: the single skip's 409 (`recovery_changed`, a bare code the server never
 * translates) is told in the operator's language, not as the code.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { QueueCard } from '../../components/QueueCard';
import { QueueTimelineView } from '../../components/Queue/QueueTimelineView';
import type { PrinterQueue, PrintQueueItem } from '../../api/client';

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
  pending_count: 2,
  completed_count: 0,
  failed_count: 0,
  cancelled_count: 0,
  skipped_count: 0,
  total_count: 2,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-22T00:00:00Z',
};

const copy = (id: number, queueId = 7) => ({
  id,
  queue_id: queueId,
  printer_id: queueId,
  archive_id: 40 + id,
  position: id,
  status: 'pending',
  archive_name: 'Bracket',
  printer_name: `A1-0${queueId}`,
  source_storage: 'ready',
  batch_id: 'shared-batch',
  manual_start: false,
  scheduled_time: null,
  print_time_seconds: 600,
});

/** Every batch request the card sends, as path + query. */
let batchCalls: URL[];

beforeEach(() => {
  batchCalls = [];
  localStorage.removeItem('queueCollapsedBatches');
  server.use(
    http.get('/api/v1/queue/', ({ request }) => {
      const status = new URL(request.url).searchParams.get('status');
      return HttpResponse.json(status === 'pending' ? [copy(1), copy(2)] : []);
    }),
    http.get('/api/v1/queue/summary', () => HttpResponse.json({ pending_count: 2, groups: [] })),
    http.get('/api/v1/printers/7/status', () => HttpResponse.json({ state: 'IDLE' })),
    http.post('/api/v1/queue/batch/:batchId/cancel', ({ request }) => {
      batchCalls.push(new URL(request.url));
      return HttpResponse.json({ cancelled: 2, batch_id: 'shared-batch', queue_id: 7 });
    }),
    http.post('/api/v1/queue/batch/:batchId/ungroup', ({ request }) => {
      batchCalls.push(new URL(request.url));
      return HttpResponse.json({ ungrouped: 2, batch_id: 'shared-batch', queue_id: 7 });
    }),
    http.post('/api/v1/queue/batch/:batchId/clone', ({ request }) => {
      batchCalls.push(new URL(request.url));
      return HttpResponse.json({ cloned: 2, scope: 'batch', source_batch_id: 'shared-batch', new_batch_id: 'b2', queue_id: 7 });
    }),
  );
});

async function openRowMenu() {
  const more = await screen.findAllByTitle('More');
  fireEvent.click(more[0]);
}

describe('the queue card', () => {
  it.each([
    ['Cancel entire batch', '/api/v1/queue/batch/shared-batch/cancel'],
    ['Ungroup batch', '/api/v1/queue/batch/shared-batch/ungroup'],
    ['Clone entire batch', '/api/v1/queue/batch/shared-batch/clone'],
  ])('«%s» acts on the card’s own queue', async (label, path) => {
    render(<QueueCard queue={queue} onEditItem={vi.fn()} />);
    await openRowMenu();
    fireEvent.click(await screen.findByRole('button', { name: label }));

    await waitFor(() => expect(batchCalls).toHaveLength(1));
    expect(batchCalls[0].pathname).toBe(path);
    expect(batchCalls[0].searchParams.get('queue_id')).toBe('7');
  });

  it('shows the server’s sentence when a batch still spans queues', async () => {
    server.use(
      http.post('/api/v1/queue/batch/:batchId/cancel', () =>
        HttpResponse.json(
          { detail: { error: 'batch_spans_queues', message: 'This batch has jobs in several printer queues. Choose the printer queue to act on.' } },
          { status: 409 },
        )),
    );
    render(<QueueCard queue={queue} onEditItem={vi.fn()} />);
    await openRowMenu();
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel entire batch' }));

    expect(await screen.findByText('This batch has jobs in several printer queues. Choose the printer queue to act on.'))
      .toBeInTheDocument();
  });

  it('tells a skip the scheduler overtook in words, not as its code', async () => {
    server.use(
      http.post('/api/v1/queue/:id/skip', () => HttpResponse.json({ detail: 'recovery_changed' }, { status: 409 })),
    );
    render(<QueueCard queue={queue} onEditItem={vi.fn()} />);
    await openRowMenu();
    fireEvent.click(await screen.findByRole('button', { name: 'Skip' }));

    expect(await screen.findByText(/changed before it could be skipped/)).toBeInTheDocument();
    expect(screen.queryByText('recovery_changed')).not.toBeInTheDocument();
  });
});

describe('the timeline', () => {
  it('cancels the lane’s copies of a batch through the batch route, naming that lane’s queue', async () => {
    const otherQueue: PrinterQueue = { ...queue, id: 8, printer_id: 8, printer_name: 'A1-08' };
    // One AutoQueue batch spread over two printers: two copies on 7, one on 8. Pinned to a
    // later start, because the timeline draws only committed work.
    const later = new Date(Date.now() + 3_600_000).toISOString();
    const items = [copy(1), copy(2), copy(3, 8)].map(row => ({ ...row, scheduled_time: later })) as unknown as PrintQueueItem[];
    server.use(http.get('/api/v1/printers/', () => HttpResponse.json([])));
    render(<QueueTimelineView queues={[queue, otherQueue]} items={items} onEditItem={vi.fn()} />);

    // A slot's tooltip names the job and its printer; the label itself may be too narrow to show.
    // (Testing Library folds the tooltip's line breaks into spaces.)
    const slots = await screen.findAllByTitle(/^Bracket A1-07 /);
    fireEvent.contextMenu(slots[0]);
    // The lane's count — its own two copies, not the batch's three.
    fireEvent.click(await screen.findByText('Cancel batch (2)'));

    await waitFor(() => expect(batchCalls).toHaveLength(1));
    expect(batchCalls[0].pathname).toBe('/api/v1/queue/batch/shared-batch/cancel');
    expect(batchCalls[0].searchParams.get('queue_id')).toBe('7');
  });
});
