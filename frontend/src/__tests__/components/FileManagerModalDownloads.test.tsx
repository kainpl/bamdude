/**
 * The file manager's downloads and range selection (G10 D, M21, M22).
 *
 * A download is a job on the server: the dialog creates it, polls it and hands
 * the result to the browser's own download — never a Blob.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerModal } from '../../components/FileManagerModal';
import type { DownloadJob } from '../../api/client';
import { startNativeDownload } from '../../utils/nativeDownload';

vi.mock('../../utils/nativeDownload', () => ({ startNativeDownload: vi.fn() }));
vi.mock('../../components/ModelViewer', () => ({ ModelViewer: () => <div /> }));
vi.mock('../../components/GcodePreview', () => ({ GcodePreview: () => <div /> }));

const FILES = ['a.gcode', 'b.gcode', 'c.gcode', 'd.gcode', 'e.gcode'].map((name, index) => ({
  name,
  path: `/${name}`,
  size: (index + 1) * 100,
  is_directory: false,
  mtime: `2026-11-1${index}T10:00:00Z`,
}));

function job(overrides: Partial<DownloadJob> = {}): DownloadJob {
  return {
    job_id: 'job-1',
    kind: 'printer',
    phase: 'preparing',
    as_zip: false,
    filename: 'a.gcode',
    requested: 1,
    succeeded: 0,
    failed: 0,
    bytes_received: 0,
    bytes_expected: 100,
    current: 'a.gcode',
    partial: false,
    failures: [],
    error: null,
    size: null,
    expires_in: null,
    ...overrides,
  };
}

/** The checkbox button of a file row. */
function checkbox(name: string): HTMLElement {
  const row = screen.getByText(name).closest('div.flex') as HTMLElement;
  return within(row).getAllByRole('button')[0];
}

describe('FileManagerModal downloads', () => {
  const onClose = vi.fn();
  let listing: Record<string, unknown>;
  let created: Record<string, unknown>[];
  let current: DownloadJob | null;
  let existing: DownloadJob[];
  let minted: Array<{ accept_partial: boolean }>;
  let cancelled: number;

  beforeEach(() => {
    vi.clearAllMocks();
    listing = { status: 'complete', files: FILES };
    created = [];
    current = null;
    existing = [];
    minted = [];
    cancelled = 0;
    server.use(
      http.get('/api/v1/printers/:id/files', () => HttpResponse.json(listing)),
      http.get('/api/v1/printers/:id/storage', () => HttpResponse.json({ used_bytes: 1, free_bytes: 1 })),
      http.get('/api/v1/printers/:id/files/download-jobs', () => HttpResponse.json({ jobs: existing })),
      http.post('/api/v1/printers/:id/files/download-jobs', async ({ request }) => {
        created.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(current, { status: 202 });
      }),
      http.get('/api/v1/printers/:id/files/download-jobs/:job', () =>
        current ? HttpResponse.json(current) : HttpResponse.json({ detail: 'Download not found' }, { status: 404 }),
      ),
      http.delete('/api/v1/printers/:id/files/download-jobs/:job', () => {
        cancelled += 1;
        current = current ? { ...current, phase: current.phase === 'ready' ? 'expired' : 'cancelling' } : null;
        return HttpResponse.json(current);
      }),
      http.post('/api/v1/printers/:id/files/download-jobs/:job/download-token', async ({ request }) => {
        minted.push((await request.json()) as { accept_partial: boolean });
        return HttpResponse.json({ url: '/api/v1/media-downloads/grant-1/a.gcode', expires_in: 120 });
      }),
    );
  });

  const open = async () => {
    render(<FileManagerModal printerId={1} printerName="X1 Carbon" onClose={onClose} />);
    await waitFor(() => expect(screen.getByText('a.gcode')).toBeInTheDocument());
  };

  it('selects the visible range between a plain click and a Shift-click', async () => {
    await open();
    fireEvent.click(checkbox('b.gcode'));
    fireEvent.click(checkbox('d.gcode'), { shiftKey: true });
    expect(screen.getByText('3 selected')).toBeInTheDocument();
    // A second Shift-click re-aims the range from the same anchor.
    fireEvent.click(checkbox('a.gcode'), { shiftKey: true });
    expect(screen.getByText('4 selected')).toBeInTheDocument();
  });

  it('ranges over the list as it is shown: sorted and filtered', async () => {
    await open();
    // Largest first: e, d, c, b, a. The range from d to b is d, c, b.
    fireEvent.change(screen.getByLabelText('Sort files'), { target: { value: 'size-desc' } });
    fireEvent.click(checkbox('d.gcode'));
    fireEvent.click(checkbox('b.gcode'), { shiftKey: true });
    expect(screen.getByText('3 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Deselect All'));

    // The filter leaves one file on screen: a Shift-click cannot reach the hidden ones,
    // and "select all" takes only what is shown.
    fireEvent.click(checkbox('a.gcode'));
    fireEvent.change(screen.getByPlaceholderText('Filter files...'), { target: { value: 'e.g' } });
    await waitFor(() => expect(screen.queryByText('b.gcode')).not.toBeInTheDocument());
    fireEvent.click(checkbox('e.gcode'), { shiftKey: true });
    expect(screen.getByText('2 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Deselect All'));
    fireEvent.click(screen.getByText('Select All'));
    expect(screen.getByText('1 selected')).toBeInTheDocument();
  });

  it('drops a selected file only when a complete listing no longer has it', async () => {
    await open();
    fireEvent.click(checkbox('a.gcode'));
    fireEvent.click(checkbox('b.gcode'));
    // The printer did not answer: the last list stays, and so does the selection.
    listing = { status: 'unavailable', stale: true, files: FILES };
    fireEvent.click(screen.getByTitle('Refresh'));
    await waitFor(() => expect(screen.getByTestId('printer-files-stale')).toBeInTheDocument());
    expect(screen.getByText(/2 selected/)).toBeInTheDocument();
    expect(screen.getByText(/list not confirmed/)).toBeInTheDocument();
    // Acting on an unconfirmed list is refused; nothing is started.
    fireEvent.click(screen.getByRole('button', { name: /^Download/ }));
    expect(created).toEqual([]);

    // A complete answer without b.gcode: now it leaves the selection.
    listing = { status: 'complete', files: FILES.filter((file) => file.name !== 'b.gcode') };
    fireEvent.click(screen.getByTitle('Refresh'));
    await waitFor(() => expect(screen.getByText('1 selected')).toBeInTheDocument());
  });

  it('starts a job, shows it, and saves the finished file through the browser', async () => {
    current = job();
    await open();
    fireEvent.click(checkbox('a.gcode'));
    fireEvent.click(screen.getByRole('button', { name: /^Download/ }));
    await waitFor(() => expect(screen.getByTestId('download-job')).toHaveAttribute('data-phase', 'preparing'));
    expect(created).toEqual([{ paths: ['/a.gcode'], storage: 'external', sizes: { '/a.gcode': 100 }, as_zip: false }]);
    expect(startNativeDownload).not.toHaveBeenCalled();

    current = job({ phase: 'ready', succeeded: 1, size: 100, current: null });
    await waitFor(() => expect(startNativeDownload).toHaveBeenCalledWith('/api/v1/media-downloads/grant-1/a.gcode'), {
      timeout: 4000,
    });
    expect(minted).toEqual([{ accept_partial: false }]);
    expect(screen.getByTestId('download-job')).toHaveAttribute('data-phase', 'ready');
  });

  it('asks for several files as one archive', async () => {
    current = job({ as_zip: true, requested: 2, filename: 'X1 Carbon-files.zip' });
    await open();
    fireEvent.click(checkbox('a.gcode'));
    fireEvent.click(checkbox('b.gcode'));
    fireEvent.click(screen.getByRole('button', { name: /^Download/ }));
    await waitFor(() => expect(created.length).toBe(1));
    expect(created[0]).toMatchObject({ paths: ['/a.gcode', '/b.gcode'], as_zip: true, filename: 'X1 Carbon-files.zip' });
  });

  it('never saves a partial result without a confirmation', async () => {
    current = job({
      phase: 'ready',
      as_zip: true,
      requested: 2,
      succeeded: 1,
      failed: 1,
      partial: true,
      failures: [{ name: 'b.gcode', status: 'not_readable', reason: '550' }],
    });
    await open();
    fireEvent.click(checkbox('a.gcode'));
    fireEvent.click(checkbox('b.gcode'));
    fireEvent.click(screen.getByRole('button', { name: /^Download/ }));
    await waitFor(() => expect(screen.getByText('1 of 2 files could not be downloaded')).toBeInTheDocument());
    expect(within(screen.getByTestId('download-failures')).getByText('not found or not readable')).toBeInTheDocument();
    expect(startNativeDownload).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save the rest' }));
    await waitFor(() => expect(startNativeDownload).toHaveBeenCalledTimes(1));
    expect(minted).toEqual([{ accept_partial: true }]);
  });

  it('says why a job failed, by its code', async () => {
    current = job({ phase: 'failed', error: 'limit_exceeded' });
    await open();
    fireEvent.click(checkbox('a.gcode'));
    fireEvent.click(screen.getByRole('button', { name: /^Download/ }));
    await waitFor(() =>
      expect(screen.getByText('The selection is larger than one download may be. Select fewer files.')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByTestId('download-job')).not.toBeInTheDocument();
  });

  it('picks up a download that was left running, without saving it by itself', async () => {
    current = job({ phase: 'ready', succeeded: 1, size: 100 });
    existing = [current];
    await open();
    await waitFor(() => expect(screen.getByTestId('download-job')).toHaveAttribute('data-phase', 'ready'));
    expect(startNativeDownload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(startNativeDownload).toHaveBeenCalledTimes(1));
  });

  it('asks before closing over a running download, and closing is not a cancel', async () => {
    current = job();
    existing = [current];
    await open();
    await waitFor(() => expect(screen.getByTestId('download-job')).toBeInTheDocument());

    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.getByText('A download is in progress')).toBeInTheDocument());
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Keep downloading' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(cancelled).toBe(0);
  });

  it('cancels on the server when the operator says so, and shows the phase the server reports', async () => {
    current = job();
    existing = [current];
    await open();
    await waitFor(() => expect(screen.getByTestId('download-job')).toBeInTheDocument());
    fireEvent.click(within(screen.getByTestId('download-job')).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.getByTestId('download-job')).toHaveAttribute('data-phase', 'cancelling'));
    expect(cancelled).toBe(1);
    // Not "cancelled" until the server says the transfer has stopped.
    expect(screen.queryByText('The download was cancelled.')).not.toBeInTheDocument();
    current = job({ phase: 'cancelled' });
    await waitFor(() => expect(screen.getByText('The download was cancelled.')).toBeInTheDocument(), { timeout: 4000 });
  });
});
