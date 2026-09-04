/**
 * Which caches an order mutation moves — decided once, for every call site.
 *
 * ⚠️ **This file exists because the lists disagreed.** Every order mutation
 * used to carry its own hand-written set of keys, and no two were the same:
 * adding a line forgot `project-plan`, the order page forgot
 * `project-archives`, the customer page forgot `customers`, and several forgot
 * the customer keys altogether. The symptom is always a figure that is right
 * after a reload and wrong before one, and it is always blamed on the server.
 * A new order mutation calls `invalidateOrderViews` and is done; it does not
 * get to have an opinion about the list.
 *
 * ⚠️ **Every key is invalidated as a PREFIX, deliberately.** An archive
 * re-filed from one order to another leaves the order it LEFT wrong too, and
 * the call site that moved it usually knows only where it landed. The same
 * goes for `customer`: an order can move between customers. Invalidating a
 * prefix costs nothing off the pages that read it — TanStack refetches only
 * queries that are currently *active*, and these six are mounted nowhere else.
 *
 * ⚠️ **On an order page it is NOT free**, and one key needs care because of
 * it: `project-plan` carries the operator's unsaved counts, so `PlanBlock`
 * reseeds on the plan's CONTENT rather than on the fact of a refetch. A key
 * added to this list that holds unsaved edits needs the same treatment.
 */

import type { QueryClient } from '@tanstack/react-query';

/**
 * The six caches an order mutation can move, as key prefixes.
 *
 * Exported as a list because `useWebSocket` cannot call the helper: its
 * invalidations are debounced and staggered through one shared timer, so it
 * needs the keys rather than the calls. One list, two consumers — the point of
 * the whole file is that there is no second copy.
 */
export const ORDER_VIEW_KEYS = [
  'projects', // the order cards' roll-up
  'project', // an order page's own figures — the prefix, see above
  'project-archives', // the Prints grid
  'project-plan', // pass 3: what is still to print
  'customers', // the customer tiles are computed from these orders
  'customer', // and one customer's page with them — the prefix, see above
] as const;

/** What the caller touched. Read for call-site legibility today; see below. */
export interface OrderViewScope {
  orderId?: number;
  customerId?: number;
}

/**
 * Mark every order view stale after a mutation that could have moved one.
 *
 * `opts` is accepted so a call site can say WHAT it touched, and so that
 * narrowing the invalidation later is an edit here rather than at forty call
 * sites. It is not read today: every key above is a prefix, on purpose.
 */
export function invalidateOrderViews(qc: QueryClient, opts: OrderViewScope = {}): void {
  void opts;
  for (const key of ORDER_VIEW_KEYS) {
    qc.invalidateQueries({ queryKey: [key] });
  }
}

/** The list keys each kind of deletion leaves behind. */
const DELETE_KEYS: Record<'order' | 'product' | 'customer', readonly string[]> = {
  // The customer survives the order and their totals move with it; `customer`
  // is the prefix because the page that deleted it need not be the customer's.
  order: ['projects', 'customers', 'customer'],
  // An order card renders the product's cover off the `projects` query.
  product: ['products', 'projects'],
  // The orders survive their customer and lose the denormalised name.
  customer: ['customers', 'projects'],
};

/**
 * Mark the LISTS stale after a delete — never the deleted row's detail key.
 *
 * ⚠️ **Not `invalidateOrderViews`.** Marking the deleted row's own key stale
 * asks TanStack to refetch something that no longer exists while the page is
 * still mounted, which lands a 404 in the query and can flash the error state
 * over a page that is already on its way out. What changed is the list.
 */
export function invalidateAfterDelete(qc: QueryClient, kind: 'order' | 'product' | 'customer'): void {
  for (const key of DELETE_KEYS[kind]) {
    qc.invalidateQueries({ queryKey: [key] });
  }
}
