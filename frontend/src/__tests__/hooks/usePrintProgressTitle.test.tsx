/**
 * Print progress in the browser tab (upstream #2681).
 *
 * Two things carry the risk. The **pick** — with a farm running, the tab can
 * only show one print, and "the one finishing soonest" is the answer that makes
 * the number worth glancing at. And **ownership of the tab** — the hook is
 * mounted globally, so it must stay completely inert while the preference is
 * off, and hand the title and favicon back exactly as it found them once it is
 * turned off again. Restoring to a hardcoded product name would drift from
 * index.html; it captures the real title at mount instead.
 */
import { describe, expect, it } from 'vitest';

import { pickActivePrint, type ProgressStatus } from '../../hooks/usePrintProgressTitle';

const running = (progress: number, remaining_time: number | null): ProgressStatus => ({
  state: 'RUNNING',
  progress,
  remaining_time,
});

describe('pickActivePrint', () => {
  it('returns nothing when the farm is idle', () => {
    expect(pickActivePrint([])).toBeNull();
    expect(pickActivePrint([{ state: 'IDLE', progress: 0, remaining_time: 0 }])).toBeNull();
    expect(pickActivePrint([undefined])).toBeNull();
  });

  it('ignores printers that are not RUNNING', () => {
    // A paused print is not finishing soonest — it is not finishing at all.
    const paused: ProgressStatus = { state: 'PAUSE', progress: 90, remaining_time: 5 };
    expect(pickActivePrint([paused, running(10, 60)])).toEqual(running(10, 60));
  });

  it('picks the print finishing soonest, not the furthest along', () => {
    // The distinction that makes this useful on a wall display: 90% of a long
    // print can still be hours behind 20% of a short one.
    const nearlyDone = running(90, 120);
    const shortJob = running(20, 4);

    expect(pickActivePrint([nearlyDone, shortJob])).toBe(shortJob);
  });

  it('breaks a tie on progress', () => {
    const behind = running(30, 10);
    const ahead = running(70, 10);

    expect(pickActivePrint([behind, ahead])).toBe(ahead);
  });

  it('treats a missing ETA as unknown rather than as "finishes now"', () => {
    // The backend defaults remaining_time to 0, not null, so a naive "smallest
    // wins" would let a printer that has not reported an ETA yet outrank a real
    // one — and the tab would sit at that print's percentage forever.
    const noEta = running(50, 0);
    const known = running(10, 45);

    expect(pickActivePrint([noEta, known])).toBe(known);
    expect(pickActivePrint([running(50, null), known])).toBe(known);
  });

  it('still picks a print whose ETA is unknown when it is the only one', () => {
    const only = running(50, 0);
    expect(pickActivePrint([only])).toBe(only);
  });

  it('skips a RUNNING printer that reports no progress at all', () => {
    const noProgress: ProgressStatus = { state: 'RUNNING', progress: null, remaining_time: 5 };
    expect(pickActivePrint([noProgress])).toBeNull();
  });
});
