import type { LibraryGroupingMetadata } from '../api/client';

/** One plate of one file — the unit a queue run distributes. */
export interface PlateUnit {
  fileId: number;
  fileName: string;
  plateIndex: number;
  /** What makes two units the SAME dialog.
   *
   *  ⚠️ Not always the file. A selection expands one file into its plates, and
   *  they belong in one mount with all of them ticked — so their key is the
   *  file. A copy run's units are queue items, and one file legitimately sits in
   *  a queue twice: keying those on the file would collapse two wanted copies
   *  into one. */
  memberKey: string;
}

/** Plates whose dialog would ask the same questions and get the same answers. */
export interface QueueGroup {
  /** Opaque; equal keys mean interchangeable units. Never parsed by callers. */
  key: string;
  units: PlateUnit[];
}

/**
 * Split a selection into groups that can each be answered once.
 *
 * The key is (printer model, nozzle, the plate's sorted filament TYPES, bed
 * type) — exactly the answers the Schedule dialog would give differently.
 *
 * ⚠️ **Colour is not in the key and must never be added.** Two spools of the
 * same type are interchangeable for this decision; keying on colour doubled the
 * group count on a real library (3 → 6) for no benefit the operator wanted.
 *
 * ⚠️ **A file with no plate metadata gets a group of its own.** It was never
 * parsed, so nothing is known about it — and an unknown answer is not a
 * matching answer. It still yields one unit (plate 1) so the operator is asked
 * about it rather than losing it silently.
 */
export function groupSelection(
  files: LibraryGroupingMetadata[],
  order: number[]
): QueueGroup[] {
  const byId = new Map(files.map((f) => [f.file_id, f]));
  const groups = new Map<string, QueueGroup>();

  for (const fileId of order) {
    const file = byId.get(fileId);
    if (!file) continue;

    const plates = file.plates.length > 0
      ? file.plates
      : [{ index: 1, filament_types: [] as string[], bed_type: null }];

    for (const plate of plates) {
      const key = file.plates.length === 0
        // Ungroupable: a key nothing else can equal.
        ? `ungrouped:${file.file_id}`
        : JSON.stringify([
            file.sliced_for_model,
            file.nozzle_diameter,
            [...plate.filament_types].sort(),
            plate.bed_type ?? file.bed_type,
          ]);

      const unit: PlateUnit = {
        fileId: file.file_id,
        fileName: file.filename,
        plateIndex: plate.index,
        // Every plate of one file shares this, which is what puts them in one
        // mount with all of them ticked.
        memberKey: `library:${file.file_id}`,
      };
      const existing = groups.get(key);
      if (existing) existing.units.push(unit);
      else groups.set(key, { key, units: [unit] });
    }
  }

  // Biggest first: the biggest saving is answered first, and abandoning the run
  // leaves the smallest tail behind. Ties keep insertion order, which is the
  // operator's order.
  return [...groups.values()].sort((a, b) => b.units.length - a.units.length);
}

/** A unit whose plate was decided before the run started — a copied queue item. */
export interface DecidedUnit {
  /** The source queue item. Two copies of one file and plate differ only here. */
  itemId: number;
  fileId: number;
  fileName: string;
  source: 'library' | 'archive';
  /** Already resolved by the caller: a queue item's null plate means plate 1
   *  (`print_queue.plate_id`'s own comment). Always a number here. */
  plateIndex: number;
}

/**
 * Group units that already know their plate.
 *
 * ⚠️ **Never expands.** `groupSelection` turns a file into all of its plates,
 * which is right when nobody has chosen yet and destructive here: a copy of one
 * plate of a five-plate file would become five queued items. The plate travels
 * with the item on purpose (see `lib/copyQueue.ts`), and this must respect it.
 *
 * ⚠️ **An archive-backed unit is never looked up.** The grouping endpoint
 * answers about library files, and archive ids are a different space — a lookup
 * would borrow the answer of whichever library row shares the number. It is
 * also not worth extending: a copy run holds at most one archive-backed unit in
 * practice (the running print), because there is no bulk add from Archives.
 */
export function groupDecidedUnits(
  units: DecidedUnit[],
  metadata: LibraryGroupingMetadata[]
): QueueGroup[] {
  const byId = new Map(metadata.map((row) => [row.file_id, row]));
  const groups = new Map<string, QueueGroup>();

  for (const unit of units) {
    const row = unit.source === 'library' ? byId.get(unit.fileId) : undefined;
    const plate = row?.plates.find((p) => p.index === unit.plateIndex);
    const key =
      row && plate
        ? JSON.stringify([
            row.sliced_for_model,
            row.nozzle_diameter,
            [...plate.filament_types].sort(),
            plate.bed_type ?? row.bed_type,
          ])
        // No row, or a plate the row does not know: its own group. An unknown
        // answer is not a matching answer.
        : `undecidable:${unit.source}:${unit.fileId}:${unit.itemId}`;

    const member: PlateUnit = {
      fileId: unit.fileId,
      fileName: unit.fileName,
      plateIndex: unit.plateIndex,
      memberKey: `item:${unit.itemId}`,
    };
    const existing = groups.get(key);
    if (existing) existing.units.push(member);
    else groups.set(key, { key, units: [member] });
  }

  return [...groups.values()].sort((a, b) => b.units.length - a.units.length);
}
