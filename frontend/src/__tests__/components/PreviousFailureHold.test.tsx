/**
 * «Acknowledge failure» on the printer card (G03 D4).
 *
 * Jobs set to run only after a successful previous print wait while the printer's
 * newest finished print is an unacknowledged failure — AutoQueue work too, which has
 * no skipped row an operator could unskip. The card says so when the server's
 * `previous_print_failed` is set, and the operator who may update queues releases it.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PreviousFailureHold } from '../../components/PreviousFailureHold';
import type { Permission, PrinterQueue } from '../../api/client';

const auth = vi.hoisted(() => ({ granted: new Set<string>() }));

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({ ...actual.useAuth(), hasPermission: (p: Permission) => auth.granted.has(p) }),
  };
});

const queue = (previousPrintFailed: boolean | undefined): PrinterQueue => ({
  id: 3,
  printer_id: 3,
  printer_name: 'P1S-03',
  printer_model: 'P1S',
  status: 'idle',
  is_paused: false,
  auto_distribute_eligible: true,
  last_activity_at: null,
  current_item_id: null,
  pending_count: 1,
  completed_count: 4,
  failed_count: 1,
  cancelled_count: 0,
  skipped_count: 0,
  total_count: 6,
  previous_print_failed: previousPrintFailed,
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-19T00:00:00Z',
});

const NOTE = /The last print on this printer failed/;

describe('PreviousFailureHold', () => {
  let acknowledged: number[];

  beforeEach(() => {
    acknowledged = [];
    auth.granted = new Set(['queue:read', 'queue:update_all']);
    server.use(
      http.post('/api/v1/queues/:id/acknowledge-failure', ({ params }) => {
        acknowledged.push(Number(params.id));
        return HttpResponse.json({ acknowledged: true });
      }),
    );
  });

  it.each([false, undefined])('says nothing while no failure holds anything (%s)', (flag) => {
    render(<PreviousFailureHold queue={queue(flag)} />);
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge failure' })).not.toBeInTheDocument();
  });

  it('explains the hold, without the action, to someone who may not update queues', () => {
    auth.granted = new Set(['queue:read']);
    render(<PreviousFailureHold queue={queue(true)} />);
    expect(screen.getByText(NOTE)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge failure' })).not.toBeInTheDocument();
  });

  it('acknowledges the failure through the queue’s route and says so', async () => {
    render(<PreviousFailureHold queue={queue(true)} />);
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge failure' }));

    await waitFor(() => expect(acknowledged).toEqual([3]));
    expect(await screen.findByText('Failure acknowledged')).toBeInTheDocument();
  });

  it('says when no failure was holding anything', async () => {
    server.use(http.post('/api/v1/queues/:id/acknowledge-failure', () => HttpResponse.json({ acknowledged: false })));
    render(<PreviousFailureHold queue={queue(true)} />);
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge failure' }));

    expect(await screen.findByText('No failure was holding jobs on this printer')).toBeInTheDocument();
  });
});
