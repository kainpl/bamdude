/**
 * Grouping a selection so the dialog is answered once per group.
 *
 * Measured on a real farm's library (2026-08-31): 60 plate-units collapse to 3
 * groups, the dominant one holding 57 — the badge and arrow work queued daily,
 * today 57 dialogs. Putting colour in the key doubles the count to 6, which is
 * why colour is excluded.
 */

import { describe, it, expect } from 'vitest';
import { groupSelection, groupDecidedUnits } from '../../utils/queueGrouping';
import type { DecidedUnit } from '../../utils/queueGrouping';
import type { LibraryGroupingMetadata } from '../../api/client';

function file(
  id: number,
  over: Partial<LibraryGroupingMetadata> = {}
): LibraryGroupingMetadata {
  return {
    file_id: id,
    filename: `f${id}.gcode.3mf`,
    sliced_for_model: 'P1S',
    nozzle_diameter: 0.6,
    bed_type: 'Textured PEI Plate',
    plates: [{ index: 1, filament_types: ['PETG'], bed_type: null }],
    ...over,
  };
}

describe('what lands in one group', () => {
  it('puts identical files together', () => {
    const groups = groupSelection([file(1), file(2), file(3)], [1, 2, 3]);
    expect(groups).toHaveLength(1);
    expect(groups[0].units).toHaveLength(3);
  });

  it('a multi-plate file contributes one unit per plate', () => {
    const groups = groupSelection(
      [file(1, {
        plates: [
          { index: 1, filament_types: ['PETG'], bed_type: null },
          { index: 2, filament_types: ['PETG'], bed_type: null },
        ],
      })],
      [1]
    );
    expect(groups[0].units.map((u) => u.plateIndex)).toEqual([1, 2]);
  });

  // fileName is the only thing the dialog can show for a unit, and it crosses a
  // rename (filename -> fileName) that nothing else would catch.
  it('carries the file name onto every unit', () => {
    const groups = groupSelection([file(1, { filename: 'bracket.gcode.3mf' })], [1]);
    expect(groups[0].units[0]).toEqual({
      fileId: 1,
      fileName: 'bracket.gcode.3mf',
      plateIndex: 1,
      memberKey: 'library:1',
    });
  });
});

describe('what splits a group', () => {
  it('a different filament type does', () => {
    const other = file(2, { plates: [{ index: 1, filament_types: ['PLA'], bed_type: null }] });
    expect(groupSelection([file(1), other], [1, 2])).toHaveLength(2);
  });

  it('a different nozzle does', () => {
    expect(groupSelection([file(1), file(2, { nozzle_diameter: 0.4 })], [1, 2])).toHaveLength(2);
  });

  it('a different printer model does', () => {
    expect(groupSelection([file(1), file(2, { sliced_for_model: 'A1' })], [1, 2])).toHaveLength(2);
  });

  it('a different bed type does, and a plate overrides its file', () => {
    const other = file(2, { plates: [{ index: 1, filament_types: ['PETG'], bed_type: 'Cool Plate' }] });
    expect(groupSelection([file(1), other], [1, 2])).toHaveLength(2);
  });

  it('⚠️ a different COLOUR does not — that is half the benefit', () => {
    // Colour never reaches this function; two plates differing only by colour
    // arrive with identical filament_types and must not split.
    const red = file(1, { plates: [{ index: 1, filament_types: ['PETG'], bed_type: null }] });
    const blue = file(2, { plates: [{ index: 1, filament_types: ['PETG'], bed_type: null }] });
    expect(groupSelection([red, blue], [1, 2])).toHaveLength(1);
  });

  it('the same two types in a different slot order do not', () => {
    const a = file(1, { plates: [{ index: 1, filament_types: ['PETG', 'PLA'], bed_type: null }] });
    const b = file(2, { plates: [{ index: 1, filament_types: ['PLA', 'PETG'], bed_type: null }] });
    expect(groupSelection([a, b], [1, 2])).toHaveLength(1);
  });
});

describe('a file that cannot be grouped', () => {
  it('⚠️ never joins a neighbour — an unknown answer is not a matching one', () => {
    const unsliced = file(2, {
      sliced_for_model: null,
      nozzle_diameter: null,
      bed_type: null,
      plates: [],
    });
    const groups = groupSelection([file(1), unsliced], [1, 2]);
    expect(groups).toHaveLength(2);
    const lone = groups.find((g) => g.units[0].fileId === 2)!;
    expect(lone.units).toHaveLength(1);
    expect(lone.units[0].plateIndex).toBe(1);
  });

  // Without the per-file key, two never-parsed files both reduce to
  // [null, null, [], null] and merge — one dialog answering for two files
  // nothing is known about. The case above cannot catch it: its groupable
  // neighbour has a different key either way.
  it('⚠️ nor another ungroupable file — two unknowns are not one answer', () => {
    const blank = {
      sliced_for_model: null,
      nozzle_diameter: null,
      bed_type: null,
      plates: [],
    };
    expect(groupSelection([file(1, blank), file(2, blank)], [1, 2])).toHaveLength(2);
  });
});

describe('a selection that outlived a deletion', () => {
  // The endpoint skips ids it cannot find rather than 404ing the batch, so
  // `order` routinely names files absent from `files`. Dropping them is what
  // lets the operator queue the rest.
  it('skips an id the server returned nothing for', () => {
    const groups = groupSelection([file(1)], [1, 404]);
    expect(groups).toHaveLength(1);
    expect(groups[0].units.map((u) => u.fileId)).toEqual([1]);
  });
});

describe('order', () => {
  it('returns the biggest group first, so the biggest saving is answered first', () => {
    const odd = file(9, { plates: [{ index: 1, filament_types: ['ABS'], bed_type: null }] });
    const groups = groupSelection([odd, file(1), file(2)], [9, 1, 2]);
    expect(groups[0].units).toHaveLength(2);
    expect(groups[1].units).toHaveLength(1);
  });

  it('keeps the operator’s order inside a group', () => {
    const groups = groupSelection([file(3), file(1), file(2)], [3, 1, 2]);
    expect(groups[0].units.map((u) => u.fileId)).toEqual([3, 1, 2]);
  });
});

describe('units whose plate is already decided', () => {
  // A copy run's units come from queue items, which already carry the plate the
  // operator chose. Expanding them the way a selection is expanded would queue
  // plates nobody asked for.
  function decided(over: Partial<DecidedUnit> = {}): DecidedUnit {
    return { itemId: 1, fileId: 1, fileName: 'f1.gcode.3mf', source: 'library', plateIndex: 1, ...over };
  }

  const fivePlate = file(1, {
    plates: [1, 2, 3, 4, 5].map((index) => ({ index, filament_types: ['PETG'], bed_type: null })),
  });

  it('⚠️ never expands a file to its other plates', () => {
    const groups = groupDecidedUnits([decided({ plateIndex: 3 })], [fivePlate]);

    expect(groups).toHaveLength(1);
    expect(groups[0].units).toEqual([
      { fileId: 1, fileName: 'f1.gcode.3mf', plateIndex: 3, memberKey: 'item:1' },
    ]);
  });

  it('keeps two copies of the same file and plate as two units', () => {
    const groups = groupDecidedUnits(
      [decided({ itemId: 7 }), decided({ itemId: 8 })],
      [fivePlate]
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].units.map((u) => u.memberKey)).toEqual(['item:7', 'item:8']);
  });

  it('groups a decided unit by the filament types of ITS plate', () => {
    const mixed = file(1, {
      plates: [
        { index: 1, filament_types: ['PETG'], bed_type: null },
        { index: 2, filament_types: ['PETG', 'PLA'], bed_type: null },
      ],
    });

    const groups = groupDecidedUnits(
      [decided({ itemId: 1, plateIndex: 1 }), decided({ itemId: 2, plateIndex: 2 })],
      [mixed]
    );

    expect(groups).toHaveLength(2);
  });

  it('two units on the same plate of one file share a group', () => {
    const groups = groupDecidedUnits(
      [decided({ itemId: 1, plateIndex: 2 }), decided({ itemId: 2, plateIndex: 2 })],
      [fivePlate]
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].units).toHaveLength(2);
  });

  it('a plate the metadata does not know is its own group', () => {
    const twoPlate = file(1, {
      plates: [1, 2].map((index) => ({ index, filament_types: ['PETG'], bed_type: null })),
    });

    const groups = groupDecidedUnits(
      [decided({ itemId: 1, plateIndex: 1 }), decided({ itemId: 2, plateIndex: 9 })],
      [twoPlate]
    );

    expect(groups).toHaveLength(2);
    const lone = groups.find((g) => g.units[0].memberKey === 'item:2')!;
    expect(lone.units).toHaveLength(1);
  });

  it('a file the metadata does not carry at all is its own group', () => {
    const groups = groupDecidedUnits([decided({ itemId: 4, fileId: 99 })], [fivePlate]);

    expect(groups).toHaveLength(1);
    expect(groups[0].units[0].memberKey).toBe('item:4');
  });

  it('⚠️ never looks up an archive-backed unit, even when the ids collide', () => {
    // Archive ids and library file ids are different spaces. An archive unit
    // must not borrow the answer of the library row that happens to share its
    // number — it has no metadata here and is its own group.
    const groups = groupDecidedUnits(
      [decided({ itemId: 1, fileId: 1, source: 'library', plateIndex: 1 }),
       decided({ itemId: 2, fileId: 1, source: 'archive', plateIndex: 1 })],
      [fivePlate]
    );

    expect(groups).toHaveLength(2);
  });

  it('a queue item with no plate is plate 1, and groups with an explicit plate 1', () => {
    // print_queue.plate_id's own comment: "None = plate 1". The caller
    // normalises before building the unit, so both arrive here as 1.
    const groups = groupDecidedUnits(
      [decided({ itemId: 1, plateIndex: 1 }), decided({ itemId: 2, plateIndex: 1 })],
      [fivePlate]
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].units).toHaveLength(2);
  });

  it('orders biggest group first, like a selection run', () => {
    const mixed = file(1, {
      plates: [
        { index: 1, filament_types: ['PETG'], bed_type: null },
        { index: 2, filament_types: ['ABS'], bed_type: null },
      ],
    });

    const groups = groupDecidedUnits(
      [decided({ itemId: 1, plateIndex: 2 }),
       decided({ itemId: 2, plateIndex: 1 }),
       decided({ itemId: 3, plateIndex: 1 })],
      [mixed]
    );

    expect(groups[0].units).toHaveLength(2);
    expect(groups[1].units).toHaveLength(1);
  });
});

describe('memberKey on an expanded selection', () => {
  it('names the file, so one file mounts once however many plates it contributes', () => {
    const twoPlate = file(1, {
      plates: [1, 2].map((index) => ({ index, filament_types: ['PETG'], bed_type: null })),
    });

    const groups = groupSelection([twoPlate], [1]);

    expect(groups[0].units.map((u) => u.memberKey)).toEqual(['library:1', 'library:1']);
  });
});
