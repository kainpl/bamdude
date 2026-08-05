/**
 * The sequencer owns exactly two decisions: when to move on, and what to hand
 * back when the run stops. Everything else on screen is PrintModal's.
 *
 * These drive the REAL PrintModal rather than a stub. A stub would have to
 * encode my own belief about when it calls onSuccess and when it calls onClose
 * — and that belief is the whole mechanism here, so a stub would confirm it
 * whether or not it is true.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { QueueSequencer } from '../../components/QueueSequencer';
import type { LibraryFileListItem } from '../../api/client';

// One active printer, so PrintModal auto-selects it and the run needs no
// printer click — these tests are about the run, not about picking a machine.
const PRINTERS = [{ id: 7, name: 'Solo', model: 'X1C', is_active: true, enabled: true }];

const file = (id: number, name: string) =>
  ({
    id,
    filename: `${name.toLowerCase()}.gcode.3mf`,
    print_name: name,
    file_type: 'gcode',
    file_path: `/library/${id}`,
    file_size: 1024,
    folder_id: null,
    thumbnail_path: null,
    print_time_seconds: null,
    duplicate_count: 0,
    print_count: 0,
    file_tags: ['gcode'],
    tags: [],
    created_at: '2024-01-01T00:00:00Z',
  }) as unknown as LibraryFileListItem;

const FIRST = file(1, 'First');
const SECOND = file(2, 'Second');

let posted: unknown[] = [];

beforeEach(() => {
  posted = [];
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(PRINTERS)),
    http.get('/api/v1/queues/', () => HttpResponse.json([{ id: 7, printer_id: 7, is_paused: false }])),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    http.get('/api/v1/library/files/:id', ({ params }) =>
      HttpResponse.json({ id: Number(params.id), filename: 'x.gcode.3mf', sliced_for_model: null }),
    ),
    http.get('/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ is_multi_plate: false, plates: [] }),
    ),
    http.get('/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({ filaments: [] }),
    ),
    http.post('/api/v1/queue/', async ({ request }) => {
      posted.push(await request.json());
      return HttpResponse.json({ id: posted.length, status: 'pending' });
    }),
  );
});

/** Queue whatever file the dialog is currently showing. */
async function queueCurrentFile() {
  const submit = await screen.findByRole('button', { name: /Add to Queue/i });
  await waitFor(() => expect(submit).toBeEnabled());
  await userEvent.click(submit);
}

describe('QueueSequencer', () => {
  it('opens the next file once the current one is queued', async () => {
    const onDone = vi.fn();
    render(<QueueSequencer files={[FIRST, SECOND]} onDone={onDone} />);

    expect(await screen.findByText('First')).toBeInTheDocument();
    expect(screen.getByText('1/2')).toBeInTheDocument();

    await queueCurrentFile();

    expect(await screen.findByText('Second')).toBeInTheDocument();
    expect(screen.getByText('2/2')).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });

  it('ends the run with nothing left over when the last file is queued', async () => {
    const onDone = vi.fn();
    render(<QueueSequencer files={[FIRST]} onDone={onDone} />);

    await screen.findByText('First');
    await queueCurrentFile();

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(posted).toHaveLength(1);
  });

  it('hands every file back when the run is abandoned before anything is queued', async () => {
    // The abandoned files are what the caller puts back into the selection, so
    // "which ones" is not cosmetic — an empty list here would silently drop
    // work the operator still means to distribute.
    const onDone = vi.fn();
    render(<QueueSequencer files={[FIRST, SECOND]} onDone={onDone} />);

    await screen.findByText('First');
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));

    expect(onDone).toHaveBeenCalledWith([FIRST, SECOND]);
    expect(posted).toHaveLength(0);
  });

  it('hands back only the files still undistributed when abandoned mid-run', async () => {
    const onDone = vi.fn();
    render(<QueueSequencer files={[FIRST, SECOND]} onDone={onDone} />);

    await screen.findByText('First');
    await queueCurrentFile();
    await screen.findByText('Second');

    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));

    expect(onDone).toHaveBeenCalledWith([SECOND]);
  });

  it('shows no position badge for a single file', async () => {
    render(<QueueSequencer files={[FIRST]} onDone={vi.fn()} />);

    await screen.findByText('First');

    expect(screen.queryByText(/^\d+\/\d+$/)).not.toBeInTheDocument();
  });
});
