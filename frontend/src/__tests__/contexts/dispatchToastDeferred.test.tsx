/**
 * A direct print BamDude refused to start shows as «Not started» with its reason.
 *
 * The backend has sent `recent_event.status = 'deferred'` since the routing
 * guard landed, and the toast never knew the word: the status chip rendered the
 * raw i18n key, and the next event dropped the job from the list because only
 * completed / failed / cancelled rows were kept as history
 * (spec direct-print-silent-cancel §4.1).
 */
import { describe, expect, it } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { ToastProvider } from '../../contexts/ToastContext';

const fire = (detail: Record<string, unknown>) =>
  act(() => {
    window.dispatchEvent(new CustomEvent('background-dispatch', { detail }));
  });

const uploading = (jobId: number, printerName: string) => ({
  job_id: jobId,
  source_name: 'ur_2200_a1m.3mf',
  printer_name: printerName,
  message: 'Uploading',
});

const REASON =
  'The printer did not report its filament state in time after a reconnect, so the print was not started.';

describe('dispatch toast — a refused job', () => {
  it('shows it as «Not started» with its reason while another job still runs', async () => {
    render(<ToastProvider><div /></ToastProvider>);
    fire({
      total: 2, dispatched: 0, processing: 2, completed: 0, failed: 0,
      active_jobs: [uploading(1, 'pesduke'), uploading(2, 'pug')],
    });
    fire({
      total: 2, dispatched: 0, processing: 1, completed: 0, failed: 1,
      active_jobs: [uploading(2, 'pug')],
      recent_event: {
        status: 'deferred', job_id: 1, source_name: 'ur_2200_a1m.3mf', printer_name: 'pesduke', message: REASON,
      },
    });

    expect(await screen.findByText('Not started')).toBeInTheDocument();
    expect(screen.getByText(REASON)).toBeInTheDocument();
    expect(screen.queryByText('backgroundDispatch.status.deferred')).not.toBeInTheDocument();
  });

  it('keeps the refused job listed when the next event arrives', async () => {
    render(<ToastProvider><div /></ToastProvider>);
    fire({
      total: 3, dispatched: 0, processing: 3, completed: 0, failed: 0,
      active_jobs: [uploading(1, 'pesduke'), uploading(2, 'pug'), uploading(3, 'pessirko')],
    });
    fire({
      total: 3, dispatched: 0, processing: 2, completed: 0, failed: 1,
      active_jobs: [uploading(2, 'pug'), uploading(3, 'pessirko')],
      recent_event: { status: 'deferred', job_id: 1, source_name: 'ur_2200_a1m.3mf', printer_name: 'pesduke', message: REASON },
    });
    fire({
      total: 3, dispatched: 0, processing: 2, completed: 0, failed: 1,
      active_jobs: [uploading(2, 'pug'), uploading(3, 'pessirko')],
    });

    expect(await screen.findByText('Not started')).toBeInTheDocument();
  });
});
