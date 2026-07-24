/**
 * Per-plate filament mapping — the extracted matcher (upstream #2551).
 *
 * The Print dialog used to key filament requirements on the single selected
 * plate, which is null the moment two plates are ticked, so a multi-plate
 * submission matched against the UNION of every plate's filaments. Tray
 * assignment is stateful — a tray matched to one slot is not offered to the
 * next — so two plates that share a colour on different slots competed for the
 * same tray and the loser fell through to a worse match. That one union mapping
 * then went out with every plate, and the scheduler uses a stored mapping
 * verbatim, so a plate could print in the wrong colour.
 *
 * These tests pin the property the fix depends on: mapping each plate on its own
 * gives each of them the right tray, while mapping their union does not.
 */

import { describe, it, expect } from 'vitest';
import {
  buildAmsMapping,
  buildFilamentComparison,
  buildLoadedFilaments,
} from '../../hooks/useFilamentMapping';
import type { PrinterStatus } from '../../api/client';

/** One AMS with red in slot 1 and black in slot 2. */
function twoTrayPrinter(): PrinterStatus {
  return {
    ams: [
      {
        id: 0,
        tray: [
          { id: 0, tray_type: 'PLA', tray_color: 'FF0000' },
          { id: 1, tray_type: 'PLA', tray_color: '000000' },
        ],
      },
    ],
    vt_tray: [],
  } as unknown as PrinterStatus;
}

const RED = 'FF0000';

// Plate 1 prints red on slot 1; plate 2 prints red on slot 2. Both are
// single-slot prints that happen to want the same colour on different slots.
const plate1 = { filaments: [{ slot_id: 1, type: 'PLA', color: RED, used_grams: 10 }] };
const plate2 = { filaments: [{ slot_id: 2, type: 'PLA', color: RED, used_grams: 10 }] };
// What the whole-file query returns for the same selection: the union.
const union = {
  filaments: [
    { slot_id: 1, type: 'PLA', color: RED, used_grams: 10 },
    { slot_id: 2, type: 'PLA', color: RED, used_grams: 10 },
  ],
};

function mapOne(reqs: { filaments: Array<Record<string, unknown>> }) {
  const loaded = buildLoadedFilaments(twoTrayPrinter());
  return buildAmsMapping(
    buildFilamentComparison(reqs as Parameters<typeof buildFilamentComparison>[0], loaded, {})
  );
}

describe('per-plate mapping (upstream #2551)', () => {
  it('gives each plate the red tray when mapped on its own', () => {
    // Global tray id 0 = AMS 0 slot 1 = the red spool.
    expect(mapOne(plate1)).toEqual([0]);
    // Plate 2's only slot is slot 2, so the mapping is [-1, 0]: slot 1 unused,
    // slot 2 → the red tray. Each plate independently claims the red spool,
    // which is correct — they are separate prints.
    expect(mapOne(plate2)).toEqual([-1, 0]);
  });

  it('the union mapping sends the second slot to the wrong colour', () => {
    // This is the bug the per-plate fix exists to avoid: matched together, slot 1
    // takes the only red tray and slot 2 falls through to a type-only match on
    // black (global tray id 1).
    const unionMapping = mapOne(union);
    expect(unionMapping).toEqual([0, 1]);
    // ...and plate 2, dispatched with that union mapping, would print black.
    expect(unionMapping![1]).not.toBe(0);
  });

  it('keeps per-plate manual overrides independent', () => {
    const loaded = buildLoadedFilaments(twoTrayPrinter());
    // Plate 1's slot 1 is pinned to the black tray by hand; plate 2 is untouched.
    const pinned = buildAmsMapping(buildFilamentComparison(plate1, loaded, { 1: 1 }));
    const untouched = buildAmsMapping(buildFilamentComparison(plate2, loaded, {}));
    expect(pinned).toEqual([1]);
    expect(untouched).toEqual([-1, 0]);
  });

  it('marks a manual override as manual and keeps its status honest', () => {
    const loaded = buildLoadedFilaments(twoTrayPrinter());
    const [entry] = buildFilamentComparison(plate1, loaded, { 1: 1 });
    expect(entry.isManual).toBe(true);
    // Black tray on a red requirement: the type matches, the colour does not.
    expect(entry.status).toBe('type_only');
  });

  it('returns no mapping for a plate with no filaments', () => {
    expect(mapOne({ filaments: [] })).toBeUndefined();
  });
});
