import type { TFunction } from 'i18next';
import type { Order, ProjectLine } from '../../api/client';

/** Has the line's stock moved — assembled, received, issued or written off (spec
 *  workshop-order-issue, rule 13; followups, rule 44)? From then on its ready units are only
 *  added to, through «take from stock», its kits only come down (followups, rule 42), its
 *  configuration stays, and its quantity stays above what is issued and held. */
export function stockMoved(line: ProjectLine): boolean {
  return line.assembled > 0 || line.received > 0 || line.issued > 0 || (line.written_off ?? 0) > 0;
}

/** The kits the line still holds unassembled — what the kits box edits: `from_kit_units`
 *  counts assembled kits too (spec workshop-order-issue, rule 23). */
export function liveKits(line: ProjectLine): number {
  return Math.max(0, line.from_kit_units - (line.assembled ?? 0));
}

/**
 * Why a line's configuration cannot be changed now, or `null` when it can (WS-13 E4
 * B06 / D03, R01). ONE predicate for every door onto `LineConfigDialog` — the row's
 * menu item and the edit dialog's «Change part quantities…» — so the two cannot
 * disagree. A completed order's kits shipped and a moved line's stock is only added
 * to; the server refuses both (409), this says why before the dialog opens.
 */
export function configBlockedReason(order: Pick<Order, 'status'>, line: ProjectLine, t: TFunction): string | null {
  if (order.status === 'completed') return t('orders.lineConfig.completed');
  if (order.status === 'cancelled') return t('orders.lines.reopenToConfigure');
  if (stockMoved(line)) return t('orders.lines.moved');
  return null;
}
