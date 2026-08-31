/**
 * A grouped run must not hang on a member that showed itself.
 *
 * ⚠️ This is the one run-level property a stubbed PrintModal cannot pin, and
 * the failure is silent: `handleSubmit` has two endings that never call
 * `onClose` — the low-spool ConfirmModal, which lives in the JSX a silent
 * member suppresses, and a failed or partial dispatch, which only shows a
 * toast. Either one leaves the operator staring at nothing while the sequencer
 * waits for a signal that is never coming, and takes the rest of the run with
 * it. So this file drives the REAL modal end to end and asserts the run is
 * still finishable.
 */

import { useState } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { QueueSequencer } from '../../components/QueueSequencer';
import type { SequencedFile } from '../../components/QueueSequencer';
import type { LibraryGroupingMetadata } from '../../api/client';

const PRINTERS = [{ id: 7, name: 'Solo', model: 'X1C', is_active: true, enabled: true }];

const STATUS_WITH_PETG = {
  connected: true,
  state: 'IDLE',
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: 'PETG', tray_color: 'FF0000FF', remain: 90 },
        { id: 1, tray_type: '', tray_color: '', remain: -1 },
        { id: 2, tray_type: '', tray_color: '', remain: -1 },
        { id: 3, tray_type: '', tray_color: '', remain: -1 },
      ],
    },
  ],
  vt_tray: [],
};

const meta = (id: number): LibraryGroupingMetadata => ({
  file_id: id,
  filename: `f${id}.gcode.3mf`,
  sliced_for_model: 'X1C',
  nozzle_diameter: 0.4,
  bed_type: 'textured_plate',
  plates: [{ index: 1, filament_types: ['PETG'], bed_type: 'textured_plate' }],
});

let posts: number;

beforeEach(() => {
  posts = 0;
  server.use(
    http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1), meta(2)])),
    http.get('/api/v1/printers/', () => HttpResponse.json(PRINTERS)),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(STATUS_WITH_PETG)),
    http.get('/api/v1/queues/', () => HttpResponse.json([{ id: 7, printer_id: 7, is_paused: false }])),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    http.get('/api/v1/library/files/:id', ({ params }) =>
      HttpResponse.json({ id: Number(params.id), filename: 'x.gcode.3mf', sliced_for_model: 'X1C' }),
    ),
    http.get('/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] }),
    ),
    http.get('/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] }),
    ),
  );
});

/** Callers drop the sequencer when the run ends; so does this. */
function Run({ files, onDone }: { files: SequencedFile[]; onDone: (remaining: SequencedFile[]) => void }) {
  const [live, setLive] = useState(true);
  if (!live) return null;
  return (
    <QueueSequencer
      files={files}
      onDone={(remaining) => {
        setLive(false);
        onDone(remaining);
      }}
    />
  );
}

async function queueTheOpenDialog() {
  const submit = await screen.findByRole('button', { name: /Add to Queue/i });
  await waitFor(() => expect(submit).toBeEnabled());
  await userEvent.click(submit);
}

describe('QueueSequencer anti-stall', () => {
  it('⚠️ a silent member whose dispatch fails shows itself, and the run goes on', async () => {
    // The first POST is the visible member's; the second is the silent one's
    // and fails, which is one of the two endings that never call onClose.
    server.use(
      http.post('/api/v1/queue/', () => {
        posts += 1;
        return posts === 1
          ? HttpResponse.json({ id: posts, status: 'pending' })
          : HttpResponse.json({ detail: 'printer went away' }, { status: 500 });
      }),
    );
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await screen.findByText('First');
    await queueTheOpenDialog();

    // Not a blank screen: the failed silent member stops being silent.
    expect(await screen.findByText('Second')).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });

  it('⚠️ the operator can end the run from a silent member that showed itself', async () => {
    server.use(
      http.post('/api/v1/queue/', () => {
        posts += 1;
        return posts === 1
          ? HttpResponse.json({ id: posts, status: 'pending' })
          : HttpResponse.json({ detail: 'printer went away' }, { status: 500 });
      }),
    );
    const onDone = vi.fn();
    const files = [{ id: 1, name: 'First' }, { id: 2, name: 'Second' }];
    render(<Run files={files} onDone={onDone} />);

    await screen.findByText('First');
    await queueTheOpenDialog();
    await screen.findByText('Second');

    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));

    // The one that failed is handed back, exactly like today's abandon.
    await waitFor(() => expect(onDone).toHaveBeenCalledWith([files[1]]));
  });

  it('a silent member that CAN submit never renders and never asks', async () => {
    server.use(
      http.post('/api/v1/queue/', () => {
        posts += 1;
        return HttpResponse.json({ id: posts, status: 'pending' });
      }),
    );
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await screen.findByText('First');
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(posts).toBe(2);
    expect(screen.queryByText('Second')).not.toBeInTheDocument();
  });
  it('⚠️ announces the run once for the group, not once per silent member', async () => {
    // The gate on the success toast, and the whole justification for it: a
    // member that never rendered must not announce itself, or a 57-plate group
    // would stack 57 identical cards in a corner that has no cap and no
    // de-duplication. Only the visible leader confirms, and the run's own
    // summary closes it off.
    //
    // ⚠️ Asserted on the RENDERED toast rather than through a mocked
    // ToastContext: the gate's safety rests on it being the exact negation of
    // the render guard, so the test has to see both — that the member did not
    // render AND that it did not toast. A module-scoped mock would replace the
    // provider for the whole file and could only see the second half.
    server.use(
      http.post('/api/v1/queue/', () => {
        posts += 1;
        return HttpResponse.json({ id: posts, status: 'pending' });
      }),
    );
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await screen.findByText('First');
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(posts).toBe(2);

    // Two plates queued, ONE confirmation — plus the run's own summary.
    expect(screen.getAllByText('Print queued')).toHaveLength(1);
    expect(screen.getByText('Queued 2 in 1 group')).toBeInTheDocument();
  });

  it('⚠️ a member that had to ask still announces its own submit', async () => {
    // The other direction, so a later "just suppress every toast in a grouped
    // run" simplification fails here: once a member has shown itself, it is a
    // dialog the operator answered like any other and it confirms like one.
    server.use(
      http.post('/api/v1/queue/', () => {
        posts += 1;
        // 1 = the leader, 2 = the silent member (fails, so it shows itself),
        // 3 = the operator submitting that dialog by hand.
        return posts === 2
          ? HttpResponse.json({ detail: 'printer went away' }, { status: 500 })
          : HttpResponse.json({ id: posts, status: 'pending' });
      }),
    );
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await screen.findByText('First');
    await queueTheOpenDialog();

    await screen.findByText('Second');
    expect(screen.getAllByText('Print queued')).toHaveLength(1);

    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(screen.getAllByText('Print queued')).toHaveLength(2);
  });
});
