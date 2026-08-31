import type { LibraryGroupingMetadata } from '../api/client';

/** One plate of one file — the unit a queue run distributes. */
export interface PlateUnit {
  fileId: number;
  fileName: string;
  plateIndex: number;
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
