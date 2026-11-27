/**
 * A selection queued «Run next», and a selection sliced for several printer models (N03).
 *
 * Upstream Bambuddy #3471 keeps a multi-file ASAP run in the order it was chosen in.
 * Ours places every «next» block before all pending work, so without an anchor the
 * second file of a run lands IN FRONT of the first: each block names the rows the run
 * already created, and the server puts it behind them.
 *
 * Upstream's other answer for several selected files — "alternatives", one job that the
 * first free printer of a matching model prints — is ours through Calculate (the order
 * planner splits the same parts between models). A run over files of several models
 * points there.
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

const PRINTERS = [
  { id: 7, name: 'Alpha', model: 'X1C', ip_address: '10.0.0.7', is_active: true, enabled: true },
  { id: 8, name: 'Beta', model: 'X1C', ip_address: '10.0.0.8', is_active: true, enabled: true },
];

const STATUS = {
  connected: true,
  state: 'IDLE',
  ams: [{ id: 0, tray: [{ id: 0, tray_type: 'PETG', tray_color: 'FF0000FF', remain: 90 }] }],
  vt_tray: [],
};

const meta = (id: number, model: string): LibraryGroupingMetadata => ({
  file_id: id,
  filename: `f${id}.gcode.3mf`,
  sliced_for_model: model,
  nozzle_diameter: 0.4,
  bed_type: 'textured_plate',
  plates: [{ index: 1, filament_types: ['PETG'], bed_type: 'textured_plate' }],
});

type NextBlockPost = { items: { queue_id: number; library_file_id: number }[]; after_item_ids?: number[] };

let nextBlocks: NextBlockPost[];
let createdIds: number[][];

beforeEach(() => {
  nextBlocks = [];
  createdIds = [];
  let nextId = 100;
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(PRINTERS)),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(STATUS)),
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
    http.get('/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] }),
    ),
    http.get('/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] }),
    ),
    http.post('/api/v1/queue/next-block', async ({ request }) => {
      const body = (await request.json()) as NextBlockPost;
      nextBlocks.push(body);
      const ids = body.items.map(() => nextId++);
      createdIds.push(ids);
      return HttpResponse.json(ids.map((id) => ({ id, status: 'pending' })));
    }),
  );
});

function Run({
  files,
  onDone,
  onComputeOrder,
}: {
  files: SequencedFile[];
  onDone: (remaining: SequencedFile[]) => void;
  onComputeOrder?: (fileIds: number[]) => void;
}) {
  const [live, setLive] = useState(true);
  if (!live) return null;
  return (
    <QueueSequencer
      files={files}
      onDone={(remaining) => {
        setLive(false);
        onDone(remaining);
      }}
      onComputeOrder={onComputeOrder}
    />
  );
}

describe('QueueSequencer «Run next» keeps the selection in order (N03, upstream #3471)', () => {
  it('sends each later block behind the rows the run already created', async () => {
    server.use(http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1, 'X1C'), meta(2, 'X1C')])));
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await screen.findByText('First');
    await userEvent.click(await screen.findByRole('button', { name: /Beta/ }));
    await userEvent.click(screen.getByLabelText(/Run next/i));
    const submit = await screen.findByRole('button', { name: /Add to Queue/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(nextBlocks).toHaveLength(2);
    expect(nextBlocks.map((b) => b.items[0].library_file_id)).toEqual([1, 2]);
    expect(nextBlocks[0].after_item_ids ?? []).toEqual([]);
    expect(nextBlocks[1].after_item_ids).toEqual(createdIds[0]);
  });

  /** Leader answered, «Apply to the rest» off, so the second file asks with the leader's answer seeded. */
  async function answerLeaderThenOpenSecond({ runNextOnLeader }: { runNextOnLeader: boolean }) {
    await screen.findByText('First');
    await userEvent.click(await screen.findByRole('button', { name: /Beta/ }));
    if (runNextOnLeader) await userEvent.click(screen.getByLabelText(/Run next/i));
    await userEvent.click(screen.getByLabelText(/Apply to the rest of this group/i));
    const submit = await screen.findByRole('button', { name: /Add to Queue/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);
    await screen.findByText('Second');
  }

  it('⚠️ anchors only on «Run next» rows: a file after one queued at the end still goes first (A1-01)', async () => {
    server.use(http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1, 'X1C'), meta(2, 'X1C')])));
    const endPosts: number[] = [];
    server.use(
      http.post('/api/v1/queue/', async () => {
        endPosts.push(1);
        return HttpResponse.json({ id: 900 + endPosts.length, status: 'pending', created_item_ids: [900 + endPosts.length] });
      }),
    );
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await answerLeaderThenOpenSecond({ runNextOnLeader: false });
    await userEvent.click(screen.getByLabelText(/Run next/i));
    const submit = await screen.findByRole('button', { name: /Add to Queue/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(endPosts).toHaveLength(1);
    expect(nextBlocks).toHaveLength(1);
    // The end row is no anchor: anchored on it, the «next» block would land behind all other work.
    expect(nextBlocks[0].after_item_ids ?? []).toEqual([]);
  });

  it('anchors per printer: a block on Alpha does not name the rows the run put on Beta (A1-02)', async () => {
    server.use(http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1, 'X1C'), meta(2, 'X1C')])));
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await answerLeaderThenOpenSecond({ runNextOnLeader: true });
    // The seeded answer is Beta + Run next; move this file to Alpha.
    await userEvent.click(screen.getByRole('button', { name: /Beta/ }));
    await userEvent.click(screen.getByRole('button', { name: /Alpha/ }));
    const submit = await screen.findByRole('button', { name: /Add to Queue/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(nextBlocks.map((b) => b.items[0].queue_id)).toEqual([8, 7]);
    expect(nextBlocks[1].after_item_ids ?? []).toEqual([]);
  });

  it('sends at most the 1000 newest anchors of the printer (A1-02)', async () => {
    server.use(http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1, 'X1C'), meta(2, 'X1C')])));
    let call = 0;
    server.use(
      http.post('/api/v1/queue/next-block', async ({ request }) => {
        const body = (await request.json()) as NextBlockPost;
        nextBlocks.push(body);
        call += 1;
        // The first file came back as 1005 rows (quantity), the second as one.
        const ids = call === 1 ? Array.from({ length: 1005 }, (_, i) => 1 + i) : [5000];
        createdIds.push(ids);
        return HttpResponse.json(ids.map((id) => ({ id, status: 'pending' })));
      }),
    );
    const onDone = vi.fn();
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={onDone} />);

    await screen.findByText('First');
    await userEvent.click(await screen.findByRole('button', { name: /Beta/ }));
    await userEvent.click(screen.getByLabelText(/Run next/i));
    const submit = await screen.findByRole('button', { name: /Add to Queue/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(nextBlocks[1].after_item_ids).toHaveLength(1000);
    expect(nextBlocks[1].after_item_ids).toEqual(createdIds[0].slice(-1000));
  });
});

describe('QueueSequencer points a several-model selection at Calculate', () => {
  it('offers Calculate when the files are sliced for different printer models', async () => {
    server.use(http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1, 'X1C'), meta(2, 'A1')])));
    const onDone = vi.fn();
    const onComputeOrder = vi.fn();
    const files = [{ id: 1, name: 'First' }, { id: 2, name: 'Second' }];
    render(<Run files={files} onDone={onDone} onComputeOrder={onComputeOrder} />);

    expect(await screen.findByText(/sliced for different printer models/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /^Calculate$/ }));

    await waitFor(() => expect(onComputeOrder).toHaveBeenCalledWith([1, 2]));
    // The run ends and hands back what it has not queued — all of it.
    expect(onDone).toHaveBeenCalledWith(files);
    expect(nextBlocks).toHaveLength(0);
  });

  it('says nothing for files of one model', async () => {
    server.use(http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1, 'X1C'), meta(2, 'X1C')])));
    render(
      <Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={vi.fn()} onComputeOrder={vi.fn()} />,
    );

    await screen.findByText('First');
    expect(screen.queryByText(/sliced for different printer models/i)).not.toBeInTheDocument();
  });

  it('says nothing where the caller cannot plan (no Calculate offered)', async () => {
    server.use(http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([meta(1, 'X1C'), meta(2, 'A1')])));
    render(<Run files={[{ id: 1, name: 'First' }, { id: 2, name: 'Second' }]} onDone={vi.fn()} />);

    await screen.findByText('First');
    expect(screen.queryByText(/sliced for different printer models/i)).not.toBeInTheDocument();
  });
});
