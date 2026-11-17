/**
 * The archive's video chooser (G10 D, M19, M20).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ArchiveMediaDownloadModal } from '../../components/ArchiveMediaDownloadModal';
import type { ArchiveMediaSources, ArchiveRemoteVideo, DownloadJob } from '../../api/client';
import { startNativeDownload } from '../../utils/nativeDownload';

vi.mock('../../utils/nativeDownload', () => ({ startNativeDownload: vi.fn() }));

function remote(name: string, overrides: Partial<ArchiveRemoteVideo> = {}): ArchiveRemoteVideo {
  return {
    printer_id: 7,
    storage: 'external',
    path: `/timelapse/${name}`,
    name,
    size: 1000,
    mtime: '2026-11-10T14:00:00',
    time_basis: 'unconfirmed',
    overlap: null,
    assumed: false,
    ...overrides,
  };
}

function sources(overrides: Partial<ArchiveMediaSources> = {}): ArchiveMediaSources {
  return {
    archive_id: 5,
    window: { from: '2026-11-10T08:50:00', to: '2026-11-10T11:10:00', margin_minutes: 10 },
    offset_minutes: null,
    printer_id: 7,
    local: { status: 'ok', items: [{ asset: 'asset-1', name: 'Lamp.mp4', size: 3300 }] },
    timelapse: { status: 'ok', items: [remote('video_a.mp4'), remote('video_b.mp4'), remote('video_c.mp4')] },
    camera: { status: 'empty', items: [] },
    ...overrides,
  };
}

const READY: DownloadJob = {
  job_id: 'job-9',
  kind: 'archive',
  phase: 'ready',
  as_zip: true,
  filename: 'Lamp.zip',
  requested: 2,
  succeeded: 2,
  failed: 0,
  bytes_received: 4300,
  bytes_expected: 4300,
  current: null,
  partial: false,
  failures: [],
  error: null,
  size: 4400,
  expires_in: 3600,
};

describe('ArchiveMediaDownloadModal', () => {
  const onClose = vi.fn();
  let answer: ArchiveMediaSources;
  let asked: string[];
  let created: Record<string, unknown>[];

  beforeEach(() => {
    vi.clearAllMocks();
    answer = sources();
    asked = [];
    created = [];
    server.use(
      http.get('/api/v1/archives/:id/media-sources', ({ request }) => {
        asked.push(new URL(request.url).search);
        return HttpResponse.json(answer);
      }),
      http.get('/api/v1/archives/:id/media-download-jobs', () => HttpResponse.json({ jobs: [] })),
      http.post('/api/v1/archives/:id/media-download-jobs', async ({ request }) => {
        created.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(READY, { status: 202 });
      }),
      http.post('/api/v1/archives/:id/media-download-jobs/:job/download-token', () =>
        HttpResponse.json({ url: '/api/v1/media-downloads/grant-9/Lamp.zip', expires_in: 120 }),
      ),
    );
  });

  const open = async () => {
    render(<ArchiveMediaDownloadModal archiveId={5} archiveName="Lamp" onClose={onClose} />);
    await waitFor(() => expect(screen.getByText('Lamp.mp4')).toBeInTheDocument());
  };

  it('preselects nothing and says when a time is only the printer\'s own clock', async () => {
    await open();
    expect(screen.getByText('0 files selected')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download selected' })).toBeDisabled();
    expect(screen.getAllByText('Printer time — its time zone is not confirmed')).toHaveLength(3);
    // The printer's wall clock is shown as the printer wrote it, not shifted into the viewer's zone.
    expect(screen.getAllByText(/2026-11-10 14:00/)).toHaveLength(3);
  });

  it('marks a match only for an exact time or the operator\'s own offset', async () => {
    answer = sources({
      timelapse: {
        status: 'ok',
        items: [
          remote('exact.mp4', { time_basis: 'exact', overlap: true, mtime: '2026-11-10T11:00:00' }),
          remote('assumed.mp4', { overlap: true, assumed: true }),
          remote('outside.mp4', { time_basis: 'exact', overlap: false }),
          remote('untimed.mp4', { time_basis: 'unknown', mtime: null }),
        ],
      },
    });
    await open();
    expect(screen.getByText('Recorded during this print')).toBeInTheDocument();
    expect(screen.getByText('Recorded during this print, by the offset you set')).toBeInTheDocument();
    expect(screen.getByText('Recorded at another time')).toBeInTheDocument();
    expect(screen.getByText('No recording time')).toBeInTheDocument();
    // A match is a mark, never a selection.
    expect(screen.getByText('0 files selected')).toBeInTheDocument();
  });

  it('keeps the attached video when the printer cannot be read, and says which it is', async () => {
    answer = sources({
      printer_id: null,
      timelapse: { status: 'unavailable', items: [] },
      camera: { status: 'forbidden', items: [] },
    });
    await open();
    expect(screen.getByTestId('archive-media-timelapse')).toHaveAttribute('data-status', 'unavailable');
    expect(screen.getByText('The printer did not answer. The attached video above is not affected.')).toBeInTheDocument();
    expect(screen.getByText("You do not have access to this printer's files.")).toBeInTheDocument();
    fireEvent.click(screen.getByText('Lamp.mp4'));
    expect(screen.getByText('1 file selected')).toBeInTheDocument();
    // One list could still be read, so the clock settings stay.
    expect(screen.getByTestId('archive-media-clock')).toBeInTheDocument();
  });

  it('hides the clock settings when no list of the printer can be shown at all', async () => {
    answer = sources({
      printer_id: null,
      timelapse: { status: 'no_printer', items: [] },
      camera: { status: 'no_printer', items: [] },
    });
    await open();
    expect(screen.queryByTestId('archive-media-clock')).not.toBeInTheDocument();
    expect(screen.getAllByText('The printer of this archive is no longer configured.')).toHaveLength(2);
  });

  it('asks the server again only when the offset is applied, in minutes', async () => {
    await open();
    expect(asked).toEqual(['?margin_minutes=10']);
    const field = screen.getByLabelText('Printer clock ahead of UTC, hours');
    fireEvent.change(field, { target: { value: '2' } });
    fireEvent.change(field, { target: { value: '2.5' } });
    expect(asked).toHaveLength(1); // typing lists nothing on the printer
    fireEvent.blur(field);
    await waitFor(() => expect(asked).toContain('?offset_minutes=150&margin_minutes=10'));

    fireEvent.change(field, { target: { value: 'abc' } });
    fireEvent.blur(field);
    expect(field).toHaveAttribute('aria-invalid', 'true');
    expect(asked).toHaveLength(2);

    fireEvent.change(screen.getByLabelText('Time margin'), { target: { value: '60' } });
    await waitFor(() => expect(asked).toContain('?offset_minutes=150&margin_minutes=60'));
  });

  it('selects a range with Shift inside one list and downloads what was picked', async () => {
    await open();
    const list = screen.getByTestId('archive-media-timelapse');
    fireEvent.click(within(list).getByText('video_a.mp4'));
    fireEvent.click(within(list).getByText('video_c.mp4'), { shiftKey: true });
    expect(screen.getByText('3 files selected')).toBeInTheDocument();
    fireEvent.click(within(list).getByText('Clear'));
    fireEvent.click(within(list).getByText('video_b.mp4'));
    fireEvent.click(screen.getByText('Lamp.mp4'));

    fireEvent.click(screen.getByRole('button', { name: 'Download selected' }));
    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toEqual({
      assets: ['asset-1'],
      remote: [{ printer_id: 7, storage: 'external', path: '/timelapse/video_b.mp4', size: 1000 }],
      as_zip: true,
    });
    // Complete and started here: saved by the browser itself.
    await waitFor(() => expect(startNativeDownload).toHaveBeenCalledWith('/api/v1/media-downloads/grant-9/Lamp.zip'));
    expect(screen.getByTestId('download-job')).toHaveAttribute('data-phase', 'ready');
  });

  it('shows that the list could not be loaded instead of an empty dialog', async () => {
    server.use(http.get('/api/v1/archives/:id/media-sources', () => HttpResponse.json({ detail: 'x' }, { status: 500 })));
    render(<ArchiveMediaDownloadModal archiveId={5} archiveName="Lamp" onClose={onClose} />);
    await waitFor(() => expect(screen.getByTestId('archive-media-error')).toBeInTheDocument());
  });
});
