/**
 * The sequencer's run over GROUPS: files whose dialog would be answered
 * identically are answered once, and the rest of the group is queued silently.
 *
 * ⚠️ These drive a STUB PrintModal, unlike `QueueSequencer.test.tsx`, which
 * deliberately drives the real one. The reason the two differ: that file pins
 * the `onSuccess`-then-`onClose` protocol, which a stub would only confirm
 * because I encoded my own belief about it. This file pins the opposite half —
 * WHICH props the run hands each member (`preselectedPlateIds`, `groupBadge`,
 * `autoSubmitWhenUnambiguous`) — and those are not observable through the real
 * dialog without asserting on somebody else's rendering. The protocol stays
 * pinned for real next door, and `QueueSequencerAntiStall.test.tsx` pins the
 * one run-level behaviour a stub genuinely cannot: a silent member that renders
 * instead of submitting must not hang the run.
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

// Library file ids whose SILENT member refuses to submit and shows itself
// instead — the real modal does this when the filament is unavailable, when the
// printer status query fails, on a low-spool warning or on a failed dispatch.
const control = vi.hoisted(() => ({ refuse: new Set<number>(), mounts: [] as number[] }));

vi.mock('../../components/PrintModal', async () => {
  // `createElement` rather than JSX: a `vi.mock` factory is hoisted above the
  // file's imports, and the automatic JSX runtime import is not available yet
  // when it runs.
  const { useEffect, useRef, createElement } = await import('react');

  interface StubProps {
    libraryFileId?: number;
    archiveId?: number;
    archiveName: string;
    preselectedPlateId?: number | null;
    preselectedPlateIds?: number[];
    groupBadge?: { current: number; total: number; units: number };
    sequence?: { current: number; total: number };
    autoSubmitWhenUnambiguous?: boolean;
    onAutoSubmitRefused?: () => void;
    onSuccess?: () => void;
    onClose: () => void;
  }

  function PrintModal(props: StubProps) {
    const id = (props.libraryFileId ?? props.archiveId) as number;
    const refuses = props.autoSubmitWhenUnambiguous === true && control.refuse.has(id);
    const silent = props.autoSubmitWhenUnambiguous === true && !refuses;

    // Once-only, exactly like the real modal's `autoSubmittedRef`: `onSuccess`
    // and `onClose` are inline arrows in the sequencer, so a dep array on them
    // would re-fire on every render.
    const firedRef = useRef(false);
    useEffect(() => {
      if (firedRef.current) return;
      firedRef.current = true;
      control.mounts.push(id);
      if (refuses) props.onAutoSubmitRefused?.();
      if (silent) {
        props.onSuccess?.();
        props.onClose();
      }
    });

    if (silent) return null;

    return createElement(
      'div',
      {
        'data-testid': 'print-modal',
        'data-file': String(id),
        'data-name': props.archiveName,
        'data-plates': JSON.stringify(props.preselectedPlateIds ?? null),
        'data-plate': String(props.preselectedPlateId ?? ''),
        'data-badge-current': String(props.groupBadge?.current ?? ''),
        'data-badge-total': String(props.groupBadge?.total ?? ''),
        'data-badge-units': String(props.groupBadge?.units ?? ''),
        'data-sequence': props.sequence ? `${props.sequence.current}/${props.sequence.total}` : '',
        'data-auto': String(props.autoSubmitWhenUnambiguous === true),
      },
      createElement(
        'button',
        {
          type: 'button',
          onClick: () => {
            props.onSuccess?.();
            props.onClose();
          },
        },
        'queue',
      ),
      createElement('button', { type: 'button', onClick: () => props.onClose() }, 'abandon'),
    );
  }

  return { PrintModal };
});

const file = (id: number): SequencedFile => ({ id, name: `File ${id}` });

/** One file, `plates` plates, every plate needing the same single filament. */
const meta = (id: number, filament: string, plates = 1): LibraryGroupingMetadata => ({
  file_id: id,
  filename: `f${id}.gcode.3mf`,
  sliced_for_model: 'X1C',
  nozzle_diameter: 0.4,
  bed_type: 'textured_plate',
  plates: Array.from({ length: plates }, (_, i) => ({
    index: i + 1,
    filament_types: [filament],
    bed_type: 'textured_plate',
  })),
});

function serveGrouping(rows: LibraryGroupingMetadata[]) {
  server.use(
    http.get('/api/v1/library/grouping-metadata', () => HttpResponse.json(rows)),
  );
}

/**
 * Every caller drops the sequencer the moment the run ends (`setDroppedForQueue
 * (null)`, `setQueueSequence(null)`). Left mounted, the last member goes on
 * living and its own fallback effects keep firing — which is a property of the
 * harness, not of the run. So the tests end the run the way production does.
 */
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

const dialog = () => screen.getByTestId('print-modal');
const queueIt = () => userEvent.click(screen.getByRole('button', { name: 'queue' }));
const abandonIt = () => userEvent.click(screen.getByRole('button', { name: 'abandon' }));

beforeEach(() => {
  control.refuse.clear();
  control.mounts.length = 0;
});

describe('QueueSequencer over groups', () => {
  it('answers one dialog for a group of three identical files', async () => {
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG'), meta(3, 'PETG')]);
    const onDone = vi.fn();
    render(<Run files={[file(1), file(2), file(3)]} onDone={onDone} />);

    // One group, three units — so the badge says group 1 of 1, carrying 3.
    await waitFor(() => expect(dialog()).toBeInTheDocument());
    expect(dialog().getAttribute('data-badge-current')).toBe('1');
    expect(dialog().getAttribute('data-badge-total')).toBe('1');
    expect(dialog().getAttribute('data-badge-units')).toBe('3');
    expect(dialog().getAttribute('data-auto')).toBe('false');

    await queueIt();

    // The other two never asked: the run finished off one answer.
    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    expect(control.mounts).toEqual([1, 2, 3]);
    expect(screen.queryByTestId('print-modal')).not.toBeInTheDocument();
  });

  it('opens a dialog per group, biggest first', async () => {
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG'), meta(3, 'ABS')]);
    const onDone = vi.fn();
    render(<Run files={[file(1), file(2), file(3)]} onDone={onDone} />);

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    expect(dialog().getAttribute('data-badge-units')).toBe('2');
    expect(dialog().getAttribute('data-badge-current')).toBe('1');
    expect(dialog().getAttribute('data-badge-total')).toBe('2');
    expect(dialog().getAttribute('data-file')).toBe('1');

    await queueIt();

    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('3'));
    expect(dialog().getAttribute('data-badge-units')).toBe('1');
    expect(dialog().getAttribute('data-badge-current')).toBe('2');

    await queueIt();
    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
  });

  it('shows every in-group plate of the file it opens on', async () => {
    serveGrouping([meta(1, 'PETG', 2)]);
    render(<Run files={[file(1)]} onDone={vi.fn()} />);

    // Both plates share a key, so the group holds both — and the dialog that
    // stands for the group must show both rather than queueing one silently.
    await waitFor(() => expect(dialog()).toBeInTheDocument());
    expect(dialog().getAttribute('data-plates')).toBe('[1,2]');
    expect(dialog().getAttribute('data-badge-units')).toBe('2');
  });

  it('abandoning a group returns its unqueued files and stops', async () => {
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG'), meta(3, 'ABS')]);
    const onDone = vi.fn();
    const files = [file(1), file(2), file(3)];
    render(<Run files={files} onDone={onDone} />);

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    await abandonIt();

    expect(onDone).toHaveBeenCalledWith(files);
    expect(screen.queryByTestId('print-modal')).not.toBeInTheDocument();
  });

  it('hands back only what the abandoned group still owed', async () => {
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG'), meta(3, 'ABS')]);
    const onDone = vi.fn();
    render(<Run files={[file(1), file(2), file(3)]} onDone={onDone} />);

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    await queueIt();

    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('3'));
    await abandonIt();

    expect(onDone).toHaveBeenCalledWith([file(3)]);
  });

  it('marks every member after the first as one that submits itself', async () => {
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG')]);
    control.refuse.add(2);
    render(<Run files={[file(1), file(2)]} onDone={vi.fn()} />);

    await waitFor(() => expect(dialog().getAttribute('data-auto')).toBe('false'));
    await queueIt();

    // File 2 refused to go silently, so it is on screen — and it was still
    // ASKED to go silently, which is the prop that makes the run a group.
    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('2'));
    expect(dialog().getAttribute('data-auto')).toBe('true');
  });

  it('⚠️ carries on when a silent member renders itself instead of submitting', async () => {
    // The whole run hangs on this: a member that neither closes nor submits
    // leaves a blank screen and the sequencer has no other signal. Whatever
    // made it show itself — no filament, a dead status query, a low-spool
    // warning, a failed dispatch — the run must still be finishable.
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG'), meta(3, 'PETG')]);
    control.refuse.add(2);
    const onDone = vi.fn();
    render(<Run files={[file(1), file(2), file(3)]} onDone={onDone} />);

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    await queueIt();

    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('2'));
    await queueIt();

    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
  });

  it('⚠️ lets the operator abandon from a silent member that showed itself', async () => {
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG'), meta(3, 'PETG')]);
    control.refuse.add(2);
    const onDone = vi.fn();
    render(<Run files={[file(1), file(2), file(3)]} onDone={onDone} />);

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    await queueIt();

    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('2'));
    await abandonIt();

    // File 1 is done; 2 and 3 were never queued and go back to the selection.
    expect(onDone).toHaveBeenCalledWith([file(2), file(3)]);
  });

  it('reports the run once at the end instead of once per silent member', async () => {
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG'), meta(3, 'PETG')]);
    render(<Run files={[file(1), file(2), file(3)]} onDone={vi.fn()} />);

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    await queueIt();

    expect(await screen.findByText('Queued 3 in 1 group')).toBeInTheDocument();
  });

  it('says how many members had to be asked after all', async () => {
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG'), meta(3, 'PETG')]);
    control.refuse.add(2);
    render(<Run files={[file(1), file(2), file(3)]} onDone={vi.fn()} />);

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    await queueIt();
    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('2'));
    await queueIt();

    expect(await screen.findByText(/Queued 3 in 1 group · 1 needed answering/)).toBeInTheDocument();
  });

  it('leaves a single-plate single-file run exactly as it was', async () => {
    serveGrouping([meta(1, 'PETG')]);
    const onDone = vi.fn();
    render(<Run files={[file(1)]} onDone={onDone} />);

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    // No badge, no sequence counter, no self-submit — one plain dialog.
    expect(dialog().getAttribute('data-badge-units')).toBe('');
    expect(dialog().getAttribute('data-sequence')).toBe('');
    expect(dialog().getAttribute('data-auto')).toBe('false');

    await queueIt();
    await waitFor(() => expect(onDone).toHaveBeenCalledWith([]));
    // And no run summary for a run that grouped nothing.
    expect(screen.queryByText(/Queued 1 in/)).not.toBeInTheDocument();
  });

  it('⚠️ still offers a file the server knew nothing about', async () => {
    // Unknown ids are skipped by the server, and `groupSelection` skips what
    // the server skipped — so without a fallback the file would never be
    // offered and would vanish from the run without a word.
    serveGrouping([meta(1, 'PETG')]);
    const onDone = vi.fn();
    render(<Run files={[file(1), file(9)]} onDone={onDone} />);

    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('1'));
    await queueIt();

    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('9'));
    expect(dialog().getAttribute('data-plates')).toBe('null');
  });

  it('⚠️ groups a run that already knows each plate, without ever expanding one', async () => {
    // A copy run DOES group now — the earlier refusal rested on reading
    // `plateId: null` as "this item had no plate", which the column's own
    // comment contradicts ("None = plate 1"). What stays true is that the plate
    // is already decided: it rides on the file and no metadata may add to it.
    //
    // An archive-backed item is still never looked up — an archive id is not a
    // library file id — so it stands alone whatever the library says.
    serveGrouping([meta(1, 'PETG'), meta(2, 'PETG')]);
    const onDone = vi.fn();
    render(
      <Run
        files={[
          { id: 1, name: 'A', source: 'archive', plateId: 2, itemId: 101 },
          { id: 2, name: 'B', source: 'library', plateId: 1, itemId: 102 },
        ]}
        onDone={onDone}
      />,
    );

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    // Two groups of one: the archive stands alone, the library row is its own
    // key. Each holds a single unit, so nothing was expanded.
    expect(dialog().getAttribute('data-badge-units')).toBe('1');
    // The plate came from the item, not from a plate list.
    expect(dialog().getAttribute('data-plates')).toBe('null');
    expect(dialog().getAttribute('data-plate')).toBe('2');
  });

  it('⚠️ never groups a copy run whose items simply had no plate', async () => {
    // The silent half of the rule above: `plateId: null` on a copied queue item
    // means "this item had no plate", not "nobody has decided yet". Group it and
    // a 3-plate file whose plates share a key would tick all three and queue
    // three items where the operator asked for one copy.
    serveGrouping([meta(1, 'PETG', 3)]);
    render(
      <Run files={[{ id: 1, name: 'A', source: 'library', plateId: null }]} onDone={vi.fn()} />,
    );

    await waitFor(() => expect(dialog()).toBeInTheDocument());
    expect(dialog().getAttribute('data-plates')).toBe('null');
    expect(dialog().getAttribute('data-badge-units')).toBe('');
  });

  it('falls back to a plain per-file run when the grouping call fails', async () => {
    server.use(
      http.get('/api/v1/library/grouping-metadata', () =>
        HttpResponse.json({ detail: 'nope' }, { status: 500 }),
      ),
    );
    const onDone = vi.fn();
    render(<Run files={[file(1), file(2)]} onDone={onDone} />);

    // Grouping is an optimisation. Losing it must cost the run nothing but the
    // saving — never the ability to queue.
    await waitFor(() => expect(dialog()).toBeInTheDocument());
    expect(dialog().getAttribute('data-sequence')).toBe('1/2');
    expect(dialog().getAttribute('data-auto')).toBe('false');

    await queueIt();
    await waitFor(() => expect(dialog().getAttribute('data-file')).toBe('2'));
  });
});
