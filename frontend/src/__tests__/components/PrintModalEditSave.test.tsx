/**
 * Saving an edited queue item reports and closes once (G03 D10.1).
 *
 * The update mutation toasted, called onSuccess and closed the dialog on its own,
 * and the submit handler then did all three again: two "Queue item updated" toasts,
 * two closes, and on a refusal two error toasts. The submit handler is the only
 * caller of that mutation and now the only reporter.
 *
 * A save refused because the job changed or started meanwhile (`queue_item_changed`,
 * G03 B1) reads the queue again, so the operator sees the job as it is now.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import type { PrintQueueItem } from '../../api/client';

const mockShowToast = vi.fn();
const mockInvalidateQueueViews = vi.fn();

vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return { ...actual, useToast: () => ({ ...actual.useToast(), showToast: mockShowToast }) };
});

vi.mock('../../utils/queryInvalidation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/queryInvalidation')>();
  return {
    ...actual,
    invalidateQueueViews: (...args: Parameters<typeof actual.invalidateQueueViews>) => {
      mockInvalidateQueueViews(...args);
      return actual.invalidateQueueViews(...args);
    },
  };
});

import { PrintModal } from '../../components/PrintModal';

const queueItem: PrintQueueItem = {
  id: 9,
  queue_id: 1,
  printer_id: 1,
  archive_id: 1,
  library_file_id: null,
  waiting_reason: null,
  origin: 'queue',
  nozzle_offset_cali: 'off',
  mesh_mode_fast_check: true,
  execute_swap_macros: false,
  swap_macro_events: null,
  selected_macro_ids: null,
  gcode_injection: false,
  preheat_override: 'inherit',
  preheat_chamber_target_override: null,
  position: 1,
  scheduled_time: null,
  auto_off_after: false,
  manual_start: false,
  require_previous_success: false,
  ams_mapping: null,
  plate_id: null,
  bed_levelling: 'on',
  flow_cali: 'on',
  layer_inspect: false,
  timelapse: false,
  use_ams: true,
  status: 'pending',
  started_at: null,
  completed_at: null,
  error_message: null,
  created_at: '2024-01-01T00:00:00Z',
  archive_name: 'Edit Print',
  archive_thumbnail: null,
  printer_name: 'X1 Carbon',
  print_time_seconds: 3600,
};

function renderModal() {
  const onClose = vi.fn();
  const onSuccess = vi.fn();
  render(
    <PrintModal
      mode="edit-queue-item"
      archiveId={1}
      archiveName="Edit Print"
      queueItem={queueItem}
      onClose={onClose}
      onSuccess={onSuccess}
    />,
  );
  return { onClose, onSuccess };
}

/** The toasts the dialog itself raised — the shared provider may raise none of its own here. */
const toasts = () => mockShowToast.mock.calls;

describe('PrintModal — saving an edited queue item', () => {
  beforeEach(() => {
    mockShowToast.mockReset();
    mockInvalidateQueueViews.mockReset();
    server.use(
      http.get('/api/v1/printers/', () =>
        HttpResponse.json([{ id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true }]),
      ),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] })),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    );
  });

  it('toasts, reports success and closes once', async () => {
    server.use(http.patch('/api/v1/queue/:id', () => HttpResponse.json({ ...queueItem })));
    const user = userEvent.setup();
    const { onClose, onSuccess } = renderModal();

    await user.click(await screen.findByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    // Let anything still queued behind the first close run before counting.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(toasts()).toHaveLength(1);
    expect(toasts()[0][0]).toBe('Queue item updated');
    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('shows one error toast on a refusal and stays open', async () => {
    server.use(
      http.patch('/api/v1/queue/:id', () => HttpResponse.json({ detail: 'Queue item is printing' }, { status: 409 })),
    );
    const user = userEvent.setup();
    const { onClose, onSuccess } = renderModal();

    await user.click(await screen.findByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(mockShowToast).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(toasts()).toHaveLength(1);
    expect(toasts()[0][1]).toBe('error');
    expect(toasts()[0][0]).toContain('Queue item is printing');
    expect(onClose).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(mockInvalidateQueueViews).not.toHaveBeenCalled();
  });

  it('reads the queue again when the job changed or started meanwhile', async () => {
    server.use(
      http.patch('/api/v1/queue/:id', () =>
        HttpResponse.json(
          {
            detail: {
              error: 'queue_item_changed',
              message: 'This job changed or started while it was being edited. Nothing was saved; refresh and try again.',
            },
          },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    const { onClose } = renderModal();

    await user.click(await screen.findByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(mockShowToast).toHaveBeenCalled());
    expect(toasts()[0][1]).toBe('error');
    expect(mockInvalidateQueueViews).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });
});
