/**
 * The Queue page's card draws its own «Clear plate» / «Repeat print» pair; it
 * carries the same defects counters as the printer card, through the same
 * hook, so the Queue page is not the one place without an input.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { QueuePage } from '../../pages/QueuePage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const queue = {
  id: 1,
  printer_id: 1,
  printer_name: 'X1 Carbon',
  printer_model: 'X1C',
  printer_location: null,
  printer_tags: [],
  status: 'idle',
  is_paused: false,
  auto_distribute_eligible: true,
  last_activity_at: null,
  current_item_id: null,
  pending_count: 1,
  completed_count: 5,
  failed_count: 0,
  cancelled_count: 0,
  skipped_count: 0,
  total_count: 6,
  created_at: '2026-04-14T00:00:00Z',
  updated_at: '2026-04-14T00:00:00Z',
};

const pendingItem = {
  id: 11,
  queue_id: 1,
  printer_id: 1,
  status: 'pending',
  manual_start: false,
  archive_id: 500,
  archive_name: 'next_job.gcode.3mf',
  library_file_id: null,
  position: 1,
  origin: 'queue',
  waiting_reason: null,
  scheduled_time: null,
  auto_off_after: false,
  require_previous_success: false,
  ams_mapping: null,
  plate_id: null,
  created_at: '2026-04-14T00:00:00Z',
};

const finishedAwaitingClear = {
  connected: true,
  state: 'FINISH',
  progress: 100,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
  awaiting_plate_clear: true,
};

const waitingPrint = {
  archive_id: 9,
  print_name: 'Done',
  status: 'completed',
  quantity: 2,
  defective_count: 0,
  parts: [{ id: 21, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 }],
};

function mockApi() {
  const posted: { clear: unknown | null; clearCalls: number } = { clear: null, clearCalls: 0 };
  server.use(
    http.get('/api/v1/queues/', () => HttpResponse.json([queue])),
    http.get('/api/v1/queue/', () => HttpResponse.json([pendingItem])),
    http.get('/api/v1/printers/', () => HttpResponse.json([])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(finishedAwaitingClear)),
    http.get('/api/v1/queue/forecast', () =>
      HttpResponse.json({ free_at: '2026-09-06T12:00:00Z', free_seconds: 0, unknown_prints: 0 }),
    ),
    http.get('/api/v1/printers/:id/waiting-print', () => HttpResponse.json(waitingPrint)),
    http.post('/api/v1/printers/:id/clear-plate', async ({ request }) => {
      const text = await request.text();
      posted.clearCalls += 1;
      posted.clear = text ? JSON.parse(text) : null;
      return HttpResponse.json({ success: true, message: 'Plate cleared', ledger_refused_parts: 0 });
    }),
  );
  return posted;
}

describe('the Queue page card', () => {
  beforeEach(() => localStorage.clear());

  it('offers the defects row above its pair', async () => {
    mockApi();
    render(<QueuePage />);
    expect(await screen.findByTestId('plate-defects-toggle')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Clear plate/i })).toBeInTheDocument();
  });

  it('a touched counter travels in the clear-plate body', async () => {
    const posted = mockApi();
    render(<QueuePage />);
    fireEvent.click(await screen.findByTestId('plate-defects-toggle'));
    const lid = (await screen.findByTestId('part-defective-21')) as HTMLInputElement;
    fireEvent.change(lid, { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: /Clear plate/i }));
    await waitFor(() =>
      expect(posted.clear).toEqual({ defects: { parts: [{ id: 21, defective: 1 }] } }),
    );
  });

  it('an untouched row sends no body', async () => {
    const posted = mockApi();
    render(<QueuePage />);
    await screen.findByTestId('plate-defects-toggle');
    fireEvent.click(screen.getByRole('button', { name: /Clear plate/i }));
    await waitFor(() => expect(posted.clearCalls).toBe(1));
    expect(posted.clear).toBeNull();
  });
});
