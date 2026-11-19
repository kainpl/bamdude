/**
 * The timelapse editor follows a background edit (G10 E, M23, M26).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { TimelapseEditorModal } from '../../components/TimelapseEditorModal';
import type { TimelapseEditJob } from '../../api/client';

function job(overrides: Partial<TimelapseEditJob> = {}): TimelapseEditJob {
  return {
    job_id: 'edit-1',
    phase: 'encoding',
    progress: 0.4,
    error: null,
    save_mode: 'replace',
    result: null,
    ...overrides,
  };
}

describe('TimelapseEditorModal', () => {
  const onClose = vi.fn();
  const onSave = vi.fn();
  let started: URL[];
  let bodies: (string | null)[];
  let existing: TimelapseEditJob[];
  let polled: TimelapseEditJob;
  let cancelled: number;

  beforeEach(() => {
    vi.clearAllMocks();
    started = [];
    bodies = [];
    existing = [];
    polled = job();
    cancelled = 0;
    server.use(
      http.get('/api/v1/archives/5/timelapse/info', () =>
        HttpResponse.json({ duration: 10, width: 1920, height: 1080, fps: 30, codec: 'h264', file_size: 1, has_audio: false }),
      ),
      http.get('/api/v1/archives/5/timelapse/thumbnails', () => HttpResponse.json({ thumbnails: [], timestamps: [] })),
      http.get('/api/v1/archives/5/timelapse/edit-jobs', () => HttpResponse.json({ jobs: existing })),
      http.post('/api/v1/archives/5/timelapse/process', async ({ request }) => {
        started.push(new URL(request.url));
        const text = await request.text();
        bodies.push(text === '' ? null : text);
        return HttpResponse.json(job({ phase: 'queued', progress: 0 }), { status: 202 });
      }),
      http.get('/api/v1/archives/5/timelapse/edit-jobs/edit-1', () => HttpResponse.json(polled)),
      http.delete('/api/v1/archives/5/timelapse/edit-jobs/edit-1', () => {
        cancelled += 1;
        polled = job({ phase: 'cancelling' });
        return HttpResponse.json(polled);
      }),
    );
  });

  function open() {
    return render(<TimelapseEditorModal archiveId={5} timelapseSrc="/video.mp4" onClose={onClose} onSave={onSave} />);
  }

  async function ready() {
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).not.toBeDisabled());
  }

  it('starts a replace edit and shows the server\'s progress, never 100 % of its own', async () => {
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(started).toHaveLength(1));
    expect(started[0].searchParams.get('save_mode')).toBe('replace');
    expect(started[0].searchParams.get('trim_start')).toBe('0');
    expect(started[0].searchParams.get('trim_end')).toBe('10');
    expect(bodies[0]).toBeNull();
    await waitFor(() => expect(screen.getByTestId('timelapse-edit-percent').textContent).toBe('40%'));
    expect(screen.getByText('Encoding…')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('"Save as new" asks for a new video and keeps the original', async () => {
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save as new' }));
    await waitFor(() => expect(started).toHaveLength(1));
    expect(started[0].searchParams.get('save_mode')).toBe('new');
  });

  it('sends the music as the body, with its volume and the mute switch', async () => {
    open();
    await ready();
    const music = new File(['la la la'], 'tune.mp3', { type: 'audio/mpeg' });
    fireEvent.change(screen.getByTestId('timelapse-music-input'), { target: { files: [music] } });
    fireEvent.change(await screen.findByTestId('timelapse-volume'), { target: { value: '0.3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(started).toHaveLength(1));
    expect(started[0].searchParams.get('audio_name')).toBe('tune.mp3');
    expect(started[0].searchParams.get('audio_volume')).toBe('0.3');
    expect(started[0].searchParams.get('mute')).toBe('false');
    expect(bodies[0]).toBe('la la la');
  });

  it('the mute switch works without music and reaches the request', async () => {
    open();
    await ready();
    fireEvent.click(screen.getByTestId('timelapse-mute'));
    expect(screen.getByText('The result will have no sound.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(started).toHaveLength(1));
    expect(started[0].searchParams.get('mute')).toBe('true');
  });

  it('closes and reports the save only when the server says the result is published', async () => {
    polled = job({ phase: 'publishing', progress: 0.97 });
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByText('Saving the result…')).toBeInTheDocument());
    expect(onSave).not.toHaveBeenCalled();
    polled = job({ phase: 'done', progress: 1 });
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1), { timeout: 5000 });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('picks up an edit that is already running when the editor is opened again', async () => {
    existing = [job({ progress: 0.6 })];
    polled = job({ progress: 0.6 });
    open();
    await waitFor(() => expect(screen.getByTestId('timelapse-edit-percent').textContent).toBe('60%'));
    expect(started).toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('a cancel asks the server and waits for its word', async () => {
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByText('Encoding…')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the edit' }));
    await waitFor(() => expect(cancelled).toBe(1));
    await waitFor(() => expect(screen.getByText('Stopping…')).toBeInTheDocument());
    // Still shown as stopping: the server has not said the encoder ended.
    expect(screen.getByTestId('timelapse-edit-status')).toBeInTheDocument();
    polled = job({ phase: 'cancelled' });
    await waitFor(() => expect(screen.queryByTestId('timelapse-edit-status')).not.toBeInTheDocument(), {
      timeout: 5000,
    });
    expect(onSave).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('a cancel the server refuses as too late is said, and the edit is still followed to its end', async () => {
    polled = job({ phase: 'publishing', progress: 0.97 });
    server.use(
      http.delete('/api/v1/archives/5/timelapse/edit-jobs/edit-1', () =>
        HttpResponse.json(
          { detail: { error: 'cancel_too_late', message: 'The edited video is already being saved and cannot be cancelled' } },
          { status: 409 },
        ),
      ),
    );
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByText('Saving the result…')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel the edit' }));
    await waitFor(() => expect(screen.getByTestId('timelapse-cancel-too-late')).toBeInTheDocument());
    expect(onClose).not.toHaveBeenCalled();
    polled = job({ phase: 'done', progress: 1 });
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1), { timeout: 5000 });
  });

  it('a result refused because the video changed is explained, and nothing is reported as saved', async () => {
    polled = job({ phase: 'failed', error: 'source_changed' });
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByTestId('timelapse-edit-error').textContent).toMatch(/was changed while/));
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Back to the editor' }));
    await waitFor(() => expect(screen.queryByTestId('timelapse-edit-status')).not.toBeInTheDocument());
  });

  it('an unknown failure code still gets a sentence', async () => {
    polled = job({ phase: 'failed', error: 'something_new' });
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByTestId('timelapse-edit-error').textContent).toBe('The edit failed.'));
  });

  it('an edit the server no longer knows stops being followed', async () => {
    server.use(
      http.get('/api/v1/archives/5/timelapse/edit-jobs/edit-1', () => HttpResponse.json({ detail: 'gone' }, { status: 404 })),
    );
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByTestId('timelapse-edit-error').textContent).toMatch(/no longer be followed/));
  });

  it('a start the server refuses is shown and leaves the editor usable', async () => {
    server.use(
      http.post('/api/v1/archives/5/timelapse/process', () =>
        HttpResponse.json({ detail: { error: 'edit_in_progress', message: 'This video is already being edited' } }, { status: 409 }),
      ),
    );
    open();
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).not.toBeDisabled());
    expect(screen.queryByTestId('timelapse-edit-status')).not.toBeInTheDocument();
  });
});
