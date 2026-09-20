/**
 * A grouped run on a farm with MORE THAN ONE printer.
 *
 * ⚠️ **This fixture is the one the whole seven-task suite lacked.** Every other
 * test of this feature mocks exactly one printer, and a single active printer
 * is auto-selected for the operator — which silently supplied the answer that
 * nothing was actually carrying. On a two-printer farm a silent member started
 * at no printer at all, so `canSubmit` was false for ever: it neither submitted
 * nor rendered, and the sequencer, which advances only on `onClose`, waited for
 * a signal that was never coming. One plate of sixty was queued and the page
 * looked idle. The reporting farm has two printers.
 *
 * So this file drives the REAL PrintModal — a stub cannot carry an answer it
 * does not build — and asserts the two halves separately:
 *   · what the operator answered reaches the members they never see;
 *   · a member that can NEVER submit shows itself instead of hanging.
 */

import { useState } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintModal } from '../../components/PrintModal';
import { QueueSequencer } from '../../components/QueueSequencer';
import type { SequencedFile } from '../../components/QueueSequencer';
import type { LibraryGroupingMetadata } from '../../api/client';

/** ⚠️ TWO active printers, so nothing is chosen for the operator. */
const PRINTERS = [
  { id: 7, name: 'Alpha', model: 'X1C', ip_address: '10.0.0.7', is_active: true, enabled: true },
  { id: 8, name: 'Beta', model: 'X1C', ip_address: '10.0.0.8', is_active: true, enabled: true },
];

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

/** One plate-unit's worth of grouping metadata per listed plate index. */
const meta = (id: number, plateIndexes: number[]): LibraryGroupingMetadata => ({
  file_id: id,
  filename: `f${id}.gcode.3mf`,
  sliced_for_model: 'X1C',
  nozzle_diameter: 0.4,
  bed_type: 'textured_plate',
  plates: plateIndexes.map((index) => ({
    index,
    filament_types: ['PETG'],
    bed_type: 'textured_plate',
  })),
});

/** A plate as the plates endpoint really returns one — the multi-plate picker
 *  renders every field, so a thinner stub crashes it rather than failing. */
const plate = (index: number) => ({
  index,
  name: `Plate ${index}`,
  has_thumbnail: false,
  thumbnail_url: null,
  objects: [],
  filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }],
  print_time_seconds: 600,
  filament_used_grams: 10,
});

type QueuePost = {
  queue_id: number;
  manual_start: boolean;
  quantity: number;
  plate_id: number | null;
  bed_levelling: string;
};

let posts: QueuePost[];

beforeEach(() => {
  posts = [];
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(PRINTERS)),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(STATUS_WITH_PETG)),
    http.get('/api/v1/queues/', () =>
      HttpResponse.json([
        { id: 7, printer_id: 7, is_paused: false },
        { id: 8, printer_id: 8, is_paused: false },
      ]),
    ),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    http.get('/api/v1/library/files/:id', ({ params }) =>
      HttpResponse.json({ id: Number(params.id), filename: 'x.gcode.3mf', sliced_for_model: 'X1C' }),
    ),
    http.post('/api/v1/queue/', async ({ request }) => {
      posts.push((await request.json()) as QueuePost);
      return HttpResponse.json({ id: posts.length, status: 'pending' });
    }),
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

async function submitTheOpenDialog() {
  const submit = await screen.findByRole('button', { name: /Add to Queue/i });
  await waitFor(() => expect(submit).toBeEnabled());
  await userEvent.click(submit);
}

describe('QueueSequencer carries the answer on a multi-printer farm', () => {
  it('⚠️ queues every member of the group, with the printer the operator picked', async () => {
    // Two single-plate files whose keys coincide: one group, one dialog, and a
    // second member that must go out without one.
    server.use(
      http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1, [1]), meta(2, [1])])),
      http.get('/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] }),
      ),
      http.get('/api/v1/library/files/:id/filament-requirements', () =>
        HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] }),
      ),
    );
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await screen.findByText('First');

    // Nothing is pre-picked with two printers on the farm — the whole point.
    await userEvent.click(await screen.findByRole('button', { name: /Beta/ }));
    // And an answer whose absence used to be actively dangerous: "Queue Only"
    // means the operator starts each job by hand. A member that defaulted back
    // to ASAP would dispatch itself the moment the printer went idle.
    await userEvent.click(screen.getByRole('button', { name: /Queue Only/i }));
    await submitTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));

    // The member never showed itself, and it went to the same printer under the
    // same schedule as the dialog that stood for it.
    expect(screen.queryByText('Second')).not.toBeInTheDocument();
    expect(posts).toHaveLength(2);
    expect(posts.map((p) => p.queue_id)).toEqual([8, 8]);
    expect(posts.map((p) => p.manual_start)).toEqual([true, true]);
  });

  it('⚠️ a member whose plates can never answer shows itself instead of hanging', async () => {
    // The same trap on a farm of any size: `perPlateReqsFailed` makes
    // `canSubmit` false for ever (the query does not retry), and multi-plate
    // members are the normal case for a grouped run. Before the self-submit
    // learned to tell "not yet" from "never", this member sat invisible and
    // took the rest of the run with it.
    server.use(
      http.get('/api/v1/library/grouping-metadata', () =>
        HttpResponse.json([meta(1, [1]), meta(2, [1, 2])]),
      ),
      http.get('/api/v1/library/files/:id/plates', ({ params }) =>
        HttpResponse.json(
          Number(params.id) === 2
            ? { is_multi_plate: true, plates: [plate(1), plate(2)] }
            : { is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] },
        ),
      ),
      http.get('/api/v1/library/files/:id/filament-requirements', ({ params }) =>
        Number(params.id) === 2
          ? HttpResponse.json({ detail: 'cannot read the 3MF' }, { status: 500 })
          : HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] }),
      ),
    );
    const onDone = vi.fn();
    const files = [{ id: 1, name: 'First' }, { id: 2, name: 'Second' }];
    render(<Run files={files} onDone={onDone} />);

    await screen.findByText('First');
    await userEvent.click(await screen.findByRole('button', { name: /Alpha/ }));
    await submitTheOpenDialog();

    // Not a blank page: the member the run cannot answer for asks.
    expect(await screen.findByText('Second')).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
    expect(posts).toHaveLength(1);

    // And the run is still finishable from there.
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith([files[1]]));
  });
});

describe('PrintModal self-submit on a multi-printer farm', () => {
  it('⚠️ reveals itself when no printer will ever be chosen for it', async () => {
    // The other permanent cause, asserted where it lives. A silent member with
    // no printer is not waiting for anything: the single-active-printer
    // auto-select is the only thing that fills an empty selection, and with two
    // printers it never fires. Rendering `null` for ever is the failure; the
    // dialog — with its printer question intact — is the fix.
    server.use(
      http.get('/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] }),
      ),
      http.get('/api/v1/library/files/:id/filament-requirements', () =>
        HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] }),
      ),
    );
    const onAutoSubmitRefused = vi.fn();
    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={1}
        archiveName="Orphan"
        autoSubmitWhenUnambiguous
        onAutoSubmitRefused={onAutoSubmitRefused}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText('Orphan')).toBeInTheDocument();
    await waitFor(() => expect(onAutoSubmitRefused).toHaveBeenCalled());
    // It asks rather than guessing, and the question is answerable: the printer
    // selector is present, which is why the carry is a separate prop and not a
    // reuse of `initialSelectedPrinterIds` (that one hides it).
    expect(screen.getByRole('button', { name: /Beta/ })).toBeInTheDocument();
    expect(posts).toHaveLength(0);
  });
});
