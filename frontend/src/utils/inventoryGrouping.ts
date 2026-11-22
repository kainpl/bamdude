import type { InventorySpool } from '../api/client';

// Synthesize a spool-shaped object for a group's collapsed header row: the
// quantity fields (label_weight, weight_used, core_weight) are summed across
// members so the header shows group totals (#1368), while identity fields are
// carried from the lowest-id member, like the server representative. Fields
// outside the group key (lot, purchase date) must not change with member order.
// The expanded per-spool rows keep using the real per-member spools.
export function aggregateGroupSpool(spools: InventorySpool[]): InventorySpool {
  const base = spools.reduce((first, spool) => spool.id < first.id ? spool : first);
  let labelWeight = 0;
  let weightUsed = 0;
  let coreWeight = 0;
  let scaleWeight: number | null = 0;
  for (const s of spools) {
    labelWeight += s.label_weight;
    weightUsed += s.weight_used;
    coreWeight += s.core_weight;
    scaleWeight = scaleWeight == null || s.last_scale_weight == null ? null : scaleWeight + s.last_scale_weight;
  }
  return {
    ...base,
    label_weight: labelWeight,
    weight_used: weightUsed,
    core_weight: coreWeight,
    last_scale_weight: scaleWeight,
    last_weighed_at: null,
  };
}
