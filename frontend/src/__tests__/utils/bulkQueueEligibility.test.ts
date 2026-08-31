/**
 * The one thing that stops a silent run: a filament TYPE with nowhere to come
 * from. Colour never does — the operator's rule, and the one the existing
 * matcher already encodes as `type_only`.
 */

import { describe, it, expect } from 'vitest';
import { canQueueWithoutAsking } from '../../utils/bulkQueueEligibility';
import type { LoadedFilament } from '../../hooks/useFilamentMapping';

const petgRed: LoadedFilament = { type: 'PETG', color: '#FF0000', globalTrayId: 1 } as LoadedFilament;
const petgBlue: LoadedFilament = { type: 'PETG', color: '#0000FF', globalTrayId: 2 } as LoadedFilament;

const needsPetgRed = [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }];
const needsPetgGreen = [{ slot_id: 1, type: 'PETG', color: '#00FF00', used_grams: 10 }];
const needsAbs = [{ slot_id: 1, type: 'ABS', color: '#FF0000', used_grams: 10 }];

describe('what the gate lets through, and what it stops', () => {
  it('an exact match is queued silently', () => {
    expect(
      canQueueWithoutAsking({ requirements: needsPetgRed, loadedFilaments: [petgRed], printerCount: 1 })
    ).toEqual({ ok: true });
  });

  it('⚠️ a colour that does not match is STILL queued silently', () => {
    expect(
      canQueueWithoutAsking({ requirements: needsPetgGreen, loadedFilaments: [petgBlue], printerCount: 1 })
    ).toEqual({ ok: true });
  });

  it('a type with no loaded spool stops the run', () => {
    expect(
      canQueueWithoutAsking({ requirements: needsAbs, loadedFilaments: [petgRed], printerCount: 1 })
    ).toEqual({ ok: false, reason: 'filament_type' });
  });
});

describe('when the gate does not apply at all', () => {
  it('⚠️ more than one printer skips the check entirely', () => {
    // The dialog would not have asked either: with a multi-printer fan-out the
    // items ship without a mapping and the scheduler computes one per plate when
    // it picks the printer. Interrupting here would ask a question nobody can
    // answer in that dialog.
    expect(
      canQueueWithoutAsking({ requirements: needsAbs, loadedFilaments: [petgRed], printerCount: 3 })
    ).toEqual({ ok: true });
  });

  it('auto-queue (no printer chosen) skips the check entirely', () => {
    expect(
      canQueueWithoutAsking({ requirements: needsAbs, loadedFilaments: [], printerCount: 0 })
    ).toEqual({ ok: true });
  });

  it('a plate that needs no filament is always fine', () => {
    expect(
      canQueueWithoutAsking({ requirements: [], loadedFilaments: [], printerCount: 1 })
    ).toEqual({ ok: true });
  });
});
