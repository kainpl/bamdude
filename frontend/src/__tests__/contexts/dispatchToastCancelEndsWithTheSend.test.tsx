/**
 * The dispatch window cancels a dispatch, and a dispatch ends when the start command is sent.
 *
 * After that there is a print, and a print is stopped from the printer's card: the Cancel
 * button stayed live through the wait for the printer's answer, where nothing read it — it
 * said «Cancelling» and the print started.
 */
import { describe, expect, it } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { ToastProvider } from '../../contexts/ToastContext';

const fire = (detail: Record<string, unknown>) =>
  act(() => {
    window.dispatchEvent(new CustomEvent('background-dispatch', { detail }));
  });

const job = (extra: Record<string, unknown> = {}) => ({
  job_id: 1,
  source_name: 'gearbox.3mf',
  printer_name: 'pug',
  message: 'Uploading',
  ...extra,
});

const state = (active: Record<string, unknown>) => ({
  total: 1, dispatched: 0, processing: 1, completed: 0, failed: 0, active_jobs: [active],
});

describe('dispatch toast — Cancel belongs to the dispatch', () => {
  it('offers Cancel while the job is being sent', async () => {
    render(<ToastProvider><div /></ToastProvider>);
    fire(state(job({ cancellable: true })));

    expect(await screen.findByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('offers Cancel to a server that does not say (an older one)', async () => {
    render(<ToastProvider><div /></ToastProvider>);
    fire(state(job()));

    expect(await screen.findByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('takes Cancel away once the start command has gone, and says where the print is stopped', async () => {
    render(<ToastProvider><div /></ToastProvider>);
    fire(state(job({ cancellable: true })));
    expect(await screen.findByRole('button', { name: 'Cancel' })).toBeInTheDocument();

    fire(state(job({ cancellable: false, message: 'Start command sent' })));

    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
    expect(screen.getByText('Command sent — stop the print from the printer card')).toBeInTheDocument();
  });
});
