import type { InventorySpool, SpoolUsageRecord } from '../api/client';

/**
 * Grouping spools into SKUs for the forecast, and the one rule that decides
 * what an archived spool still counts for.
 *
 * ⚠️ **Stock and consumption are different questions, and archiving answers
 * them differently.** An archived spool is retired, so its grams are not stock.
 * But it is also *the record of what you burned* — and on a farm the normal
 * move is: a spool runs out, you swap it, you archive the empty one so it stops
 * cluttering the list. Counting only live spools deleted that history from the
 * forecast, collapsing the rate onto whatever the fresh replacement had managed
 * to consume — almost nothing, exactly when the number mattered.
 */

export interface SkuGroup {
  key: string;
  material: string;
  subtype: string | null;
  brand: string | null;
  colorName: string | null;
  /** What you still have. Stock questions only. */
  spools: InventorySpool[];
  /** Including archived. Consumption questions only. */
  allSpools: InventorySpool[];
}

export type UsageBySpoolId = Map<number, SpoolUsageRecord[]>;

/**
 * How long a SKU with nothing but archived spools stays on the panel.
 *
 * When there is no replacement yet, that SKU is precisely the one you need to
 * reorder — and it used to vanish from the forecast at exactly that moment.
 *
 * ⚠️ **It cannot stay forever, and the age-decay does NOT solve that.** The
 * decay in the rate model is a *weighted mean* normalised by total weight, so
 * it only re-weights observations against each other; a SKU last printed two
 * years ago still reports the full rate it had back then. With zero stock that
 * reads as `daysRemaining = 0`, i.e. a permanent red stock-break alert for
 * every colour you ever stopped buying.
 *
 * 90 days = three of the rate model's 30-day half-lives.
 */
export const RECENT_SKU_WINDOW_DAYS = 90;

/**
 * When this spool last counted for anything — its newest usage record, or the
 * day it was archived.
 *
 * ⚠️ Archiving counts on its own because a spool consumed before usage history
 * existed (or whose history was cleared) has no records at all, and retiring it
 * *today* is still evidence the SKU is in play.
 */
export function lastTouchedMs(spool: InventorySpool, usageBySpoolId: UsageBySpoolId): number {
  let newest = spool.archived_at ? new Date(spool.archived_at).getTime() : 0;
  for (const r of usageBySpoolId.get(spool.id) ?? []) {
    newest = Math.max(newest, new Date(r.created_at).getTime());
  }
  return newest;
}

export function buildSkuGroups(
  spools: InventorySpool[],
  usageBySpoolId: UsageBySpoolId,
  skuKey: (m: string, s: string | null, b: string | null, c: string | null) => string,
  now: number = Date.now(),
): SkuGroup[] {
  const map = new Map<string, SkuGroup>();
  for (const spool of spools) {
    const key = skuKey(spool.material, spool.subtype, spool.brand, spool.color_name);
    const g = map.get(key) ?? {
      key,
      material: spool.material,
      subtype: spool.subtype,
      brand: spool.brand,
      colorName: spool.color_name,
      spools: [],
      allSpools: [],
    };
    g.allSpools.push(spool);
    if (!spool.archived_at) g.spools.push(spool);
    map.set(key, g);
  }

  const cutoff = now - RECENT_SKU_WINDOW_DAYS * 86400000;
  return [...map.values()].filter(
    (g) => g.spools.length > 0 || g.allSpools.some((sp) => lastTouchedMs(sp, usageBySpoolId) >= cutoff),
  );
}

/**
 * The four totals, each drawn from the list that answers its own question.
 *
 * ⚠️ `totalRemainingG` / `totalLabelG` / `totalSpools` are **stock** — archived
 * spools are gone and must never extend "days remaining". `totalUsedG` is
 * **consumption** and must survive archiving.
 *
 * `totalUsedG` follows the same baseline-aware convention as InventoryPage's
 * "Total Consumed", so the per-SKU column matches the dashboard counter.
 */
export function groupTotals(group: SkuGroup) {
  return {
    totalRemainingG: group.spools.reduce((s, sp) => s + Math.max(0, sp.label_weight - sp.weight_used), 0),
    totalLabelG: group.spools.reduce((s, sp) => s + sp.label_weight, 0),
    totalSpools: group.spools.length,
    totalUsedG: group.allSpools.reduce(
      (s, sp) => s + Math.max(0, sp.weight_used - (sp.weight_used_baseline ?? 0)),
      0,
    ),
  };
}

/**
 * Usage records backing this SKU's rate.
 *
 * ⚠️ Reset spools (`weight_used_baseline > 0`) are left out: their pre-reset
 * events have no anchor timestamp and would inflate the rate. Archived spools
 * are NOT — they are the history.
 */
export function collectGroupHistory(group: SkuGroup, usageBySpoolId: UsageBySpoolId): SpoolUsageRecord[] {
  const out: SpoolUsageRecord[] = [];
  for (const s of group.allSpools) {
    if ((s.weight_used_baseline ?? 0) === 0) out.push(...(usageBySpoolId.get(s.id) ?? []));
  }
  return out;
}
