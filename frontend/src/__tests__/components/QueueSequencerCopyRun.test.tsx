/**
 * Copying a queue groups too — without re-deciding what the queue already decided.
 *
 * ⚠️ The defect this file exists to prevent was found once already and closed
 * with a guard that kept copy runs out of grouping altogether. The guard's
 * stated reason was wrong (`plate_id: null` means PLATE 1, per the column's own
 * comment, not "this item had no plate"), but the danger behind it was real:
 * `groupSelection` expands a file into ALL its plates from metadata, which is
 * right when nobody has chosen yet and destructive for a copy, where the item
 * already carries the plate the operator picked. A copy of one plate of a
 * five-plate file must be one item, not five.
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

/** ⚠️ TWO printers. A one-printer mock hid a ship-blocking hang through seven
 *  reviews of the first grouping spec, because the single-printer auto-select
 *  silently rescued every member. */
const PRINTERS = [
  { id: 7, name: 'Solo', model: 'X1C', is_active: true, enabled: true },
  { id: 8, name: 'Duo', model: 'X1C', is_active: true, enabled: true },
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

/** One library file with five plates, all interchangeable. */
const FIVE_PLATE: LibraryGroupingMetadata = {
  file_id: 1,
  filename: 'badges.gcode.3mf',
  sliced_for_model: 'X1C',
  nozzle_diameter: 0.4,
  bed_type: 'textured_plate',
  plates: [1, 2, 3, 4, 5].map((index) => ({
    index,
    filament_types: ['PETG'],
    bed_type: 'textured_plate',
  })),
};

let posts: number;
let postedPlates: (number | null | undefined)[];
let batched: number[][];

beforeEach(() => {
  posts = 0;
  postedPlates = [];
  batched = [];
  server.use(
    http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json([FIVE_PLATE])),
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
      HttpResponse.json({ id: Number(params.id), filename: 'badges.gcode.3mf', sliced_for_model: 'X1C' }),
    ),
    http.get('/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({
        is_multi_plate: true,
        // ⚠️ PlateSelector reads `objects.length` — a plate without it crashes
        // the dialog, which looks like the run hanging.
        plates: [1, 2, 3, 4, 5].map((index) => ({
          index,
          name: `Plate ${index}`,
          objects: [],
          object_count: 0,
          has_thumbnail: false,
          thumbnail_url: null,
          print_time_seconds: 600,
          filament_used_grams: 10,
          filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10, used_meters: 3 }],
        })),
      }),
    ),
    http.get('/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] }),
    ),
    http.post('/api/v1/queue/', async ({ request }) => {
      const body = (await request.json()) as { plate_id?: number | null; quantity?: number };
      const made = Array.from({ length: body.quantity ?? 1 }, () => (posts += 1));
      postedPlates.push(body.plate_id);
      return HttpResponse.json({ id: made[0], status: 'pending', created_item_ids: made });
    }),
    http.post('/api/v1/queue/batch', async ({ request }) => {
      const body = (await request.json()) as { item_ids: number[] };
      batched.push(body.item_ids);
      return HttpResponse.json({ batch_id: 'new-batch', count: body.item_ids.length });
    }),
  );
});

/** A copy run, mounted the way QueueCard mounts one: pinned to the target. */
function CopyRun({ files, onDone }: { files: SequencedFile[]; onDone: (r: SequencedFile[]) => void }) {
  const [live, setLive] = useState(true);
  if (!live) return null;
  return (
    <QueueSequencer
      files={files}
      initialSelectedPrinterIds={[8]}
      lockPrinterSelection
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

/** What `copyableItems` produces for one queue item. */
function copied(itemId: number, plateId: number | null, batchId?: string): SequencedFile {
  return { id: 1, name: 'badges', source: 'library', plateId, itemId, batchId };
}

describe('copying a queue', () => {
  it('⚠️ a copy of ONE plate of a five-plate file queues one item, not five', async () => {
    const onDone = vi.fn();
    render(<CopyRun files={[copied(101, 3)]} onDone={onDone} />);

    await screen.findByText('badges');
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(posts).toBe(1);
    expect(postedPlates).toEqual([3]);
  });

  it('two copies of the same file and plate stay two items', async () => {
    const onDone = vi.fn();
    render(<CopyRun files={[copied(101, 3), copied(102, 3)]} onDone={onDone} />);

    await screen.findByText('badges');
    await queueTheOpenDialog();

    // One dialog for the group; the second copy follows silently.
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(posts).toBe(2);
    expect(postedPlates).toEqual([3, 3]);
  });

  it('copies of different plates that need the same filament share one dialog', async () => {
    const onDone = vi.fn();
    render(<CopyRun files={[copied(101, 1), copied(102, 4)]} onDone={onDone} />);

    await screen.findByText('badges');
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(posts).toBe(2);
    expect(postedPlates.sort()).toEqual([1, 4]);
  });

  it('a queue item with no plate is plate 1, not every plate', async () => {
    // print_queue.plate_id's own comment: "None = plate 1".
    const onDone = vi.fn();
    render(<CopyRun files={[copied(101, null)]} onDone={onDone} />);

    await screen.findByText('badges');
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(posts).toBe(1);
  });
});

describe('the per-group toggle', () => {
  it('unticking it makes the rest of the group ask, still pre-filled', async () => {
    const onDone = vi.fn();
    render(<CopyRun files={[copied(101, 3), copied(102, 3)]} onDone={onDone} />);

    await screen.findByText('badges');
    const toggle = await screen.findByRole('checkbox', { name: /apply to the rest/i });
    expect(toggle).toBeChecked();
    await userEvent.click(toggle);

    await queueTheOpenDialog();

    // The second member renders instead of submitting itself…
    const second = await screen.findByRole('button', { name: /Add to Queue/i });
    await waitFor(() => expect(second).toBeEnabled());
    expect(posts).toBe(1);

    // …and it is not blank: the leader's printer came with it.
    expect(await screen.findByText(/Duo/)).toBeInTheDocument();

    await userEvent.click(second);
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(posts).toBe(2);
  });
});

describe('the blocks the source queue had', () => {
  it('a batch of two in the source comes out as one batch of two on the target', async () => {
    // ⚠️ The source's ids mean nothing here — the target batch can only be
    // formed after its rows exist, which is why this happens at run end.
    const onDone = vi.fn();
    render(
      <CopyRun files={[copied(101, 3, 'src-a'), copied(102, 3, 'src-a')]} onDone={onDone} />,
    );

    await screen.findByText('badges');
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    await waitFor(() => expect(batched).toHaveLength(1));
    expect(batched[0]).toHaveLength(2);
  });

  it('a source batch split across two groups still comes out as one', async () => {
    // Its items need different filaments, so they are answered separately —
    // the cohort is collected across the whole run, not per group.
    server.use(
      http.get('/api/v1/library/grouping-metadata', () =>
        HttpResponse.json([
          {
            ...FIVE_PLATE,
            plates: [
              { index: 1, filament_types: ['PETG'], bed_type: 'textured_plate' },
              { index: 2, filament_types: ['ABS'], bed_type: 'textured_plate' },
            ],
          },
        ]),
      ),
    );
    const onDone = vi.fn();
    render(
      <CopyRun files={[copied(101, 1, 'src-a'), copied(102, 2, 'src-a')]} onDone={onDone} />,
    );

    await screen.findByText('badges');
    await queueTheOpenDialog();
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    await waitFor(() => expect(batched).toHaveLength(1));
    expect(batched[0]).toHaveLength(2);
  });

  it('an item that belonged to no batch forms none', async () => {
    const onDone = vi.fn();
    render(<CopyRun files={[copied(101, 3), copied(102, 3)]} onDone={onDone} />);

    await screen.findByText('badges');
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(batched).toEqual([]);
  });

  it('⚠️ a cohort the scheduler already took does not throw the run', async () => {
    // /queue/batch refuses a non-pending item, and the run's first row can be
    // dispatched before the run ends. The rows are queued either way; only the
    // block boundary is lost.
    server.use(
      http.post('/api/v1/queue/batch', () =>
        HttpResponse.json({ detail: 'Only pending items can be grouped into a batch' }, { status: 400 }),
      ),
    );
    const onDone = vi.fn();
    render(
      <CopyRun files={[copied(101, 3, 'src-a'), copied(102, 3, 'src-a')]} onDone={onDone} />,
    );

    await screen.findByText('badges');
    await queueTheOpenDialog();

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(posts).toBe(2);
  });
});
