/**
 * One place decides which caches an order mutation moves.
 *
 * ⚠️ **The bug this exists to end.** Every order mutation used to carry its own
 * hand-written list of keys, and the lists disagreed: adding a line forgot
 * `project-plan`, the archive editor forgot nothing but the order page forgot
 * `project-archives`, and half of them forgot the customer keys entirely. The
 * symptom is always the same and always blamed on the server — a figure that
 * is right after a reload and wrong before it.
 *
 * ⚠️ **A delete is NOT an invalidation of everything.** Marking the deleted
 * row's own detail key stale asks TanStack to refetch something that no longer
 * exists while the page is still mounted, which lands a 404 in the query and
 * can flash an error state over a page that is already navigating away. So the
 * delete helper touches LIST keys only — that is the whole reason it is a
 * second function rather than a flag on the first.
 */

import { describe, it, expect } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import {
  ORDER_VIEW_KEYS,
  invalidateAfterDelete,
  invalidateOrderCandidates,
  invalidateOrderViews,
} from '../../utils/queryInvalidation';

/** A query only has a state once something has put it in the cache. */
function seed(qc: QueryClient, keys: unknown[][]) {
  for (const key of keys) qc.setQueryData(key, { seeded: true });
}

function stale(qc: QueryClient, key: unknown[]) {
  return qc.getQueryState(key)?.isInvalidated === true;
}

describe('invalidateOrderViews', () => {
  it('marks every order view stale, detail keys included', () => {
    const qc = new QueryClient();
    seed(qc, [
      ['project', 5],
      ['customer', 2],
      ['projects', {}],
      ['project-plan', 5],
    ]);

    invalidateOrderViews(qc);

    expect(stale(qc, ['project', 5])).toBe(true);
    expect(stale(qc, ['customer', 2])).toBe(true);
    expect(stale(qc, ['projects', {}])).toBe(true);
    expect(stale(qc, ['project-plan', 5])).toBe(true);
  });

  it('reaches an order page other than the one that was touched', () => {
    // The prefix, not `['project', opts.orderId]`. An archive re-filed from
    // order 5 to order 6 leaves 5's figures wrong too, and the call site that
    // moved it usually knows only where it landed.
    const qc = new QueryClient();
    seed(qc, [
      ['project', 5],
      ['project-archives', 5],
      ['customer', 2],
    ]);

    invalidateOrderViews(qc, { orderId: 6, customerId: 3 });

    expect(stale(qc, ['project', 5])).toBe(true);
    expect(stale(qc, ['project-archives', 5])).toBe(true);
    expect(stale(qc, ['customer', 2])).toBe(true);
  });

  it('leaves a cache nobody asked about alone', () => {
    const qc = new QueryClient();
    seed(qc, [['products'], ['archives'], ['queue']]);

    invalidateOrderViews(qc);

    expect(stale(qc, ['products'])).toBe(false);
    expect(stale(qc, ['archives'])).toBe(false);
    expect(stale(qc, ['queue'])).toBe(false);
  });

  it('publishes the keys as a list the websocket hook can walk', () => {
    // `useWebSocket` cannot call the helper: its invalidations are debounced
    // and staggered through one shared timer, so it needs the KEYS rather than
    // the calls. Exporting the list is what keeps the two in step.
    expect([...ORDER_VIEW_KEYS]).toEqual([
      'projects',
      'project',
      'project-archives',
      'project-plan',
      'customers',
      'customer',
      'order-candidates',
    ]);
  });
});

describe('invalidateOrderCandidates', () => {
  it('marks the print dialogs’ proposal stale and leaves the order pages alone', () => {
    // ⚠️ A queue write from `PrintModal` is not an order mutation: the dialog
    // may be filing under no order at all, and sweeping every order view from
    // there would refetch pages nothing on screen is showing. What it MUST move
    // is the count the next dialog proposes — the hook caches it for 30 s.
    const qc = new QueryClient();
    seed(qc, [['order-candidates', 5, 1], ['project', 5], ['projects']]);

    invalidateOrderCandidates(qc);

    expect(stale(qc, ['order-candidates', 5, 1])).toBe(true);
    expect(stale(qc, ['project', 5])).toBe(false);
    expect(stale(qc, ['projects'])).toBe(false);
  });
});

describe('invalidateAfterDelete', () => {
  it('refreshes the lists an order leaves behind, never the order itself', () => {
    const qc = new QueryClient();
    seed(qc, [['projects'], ['customers'], ['customer', 2], ['project', 5]]);

    invalidateAfterDelete(qc, 'order');

    expect(stale(qc, ['projects'])).toBe(true);
    expect(stale(qc, ['customers'])).toBe(true);
    // The customer survives the order, so their page is stale, not gone.
    expect(stale(qc, ['customer', 2])).toBe(true);
    expect(stale(qc, ['project', 5])).toBe(false);
  });

  it('refreshes the order cards after a product goes, never the product', () => {
    // An order card renders the product's cover off the `projects` query.
    const qc = new QueryClient();
    seed(qc, [['products'], ['projects'], ['product', 7]]);

    invalidateAfterDelete(qc, 'product');

    expect(stale(qc, ['products'])).toBe(true);
    expect(stale(qc, ['projects'])).toBe(true);
    expect(stale(qc, ['product', 7])).toBe(false);
  });

  it('refreshes the orders a deleted customer leaves without one', () => {
    // The orders survive their customer and lose the denormalised name.
    const qc = new QueryClient();
    seed(qc, [['customers'], ['projects'], ['customer', 2]]);

    invalidateAfterDelete(qc, 'customer');

    expect(stale(qc, ['customers'])).toBe(true);
    expect(stale(qc, ['projects'])).toBe(true);
    expect(stale(qc, ['customer', 2])).toBe(false);
  });

  // ⚠️ **Given an id, the row's own entry is REMOVED** — still never
  // invalidated, which would refetch a 404. A LIST page passes the id because
  // nothing there is watching the deleted row's detail key, so the stale record
  // would sit in the cache until its 60 s `staleTime` ran out: click a reused
  // id, or Back into the route that just went, and the deleted thing renders
  // out of cache before any request goes out. The three DETAIL pages pass none
  // — they remove it on unmount instead (`useForgetOnUnmount`), because pulling
  // a query out from under the component still rendering it blanks the page
  // mid-navigation.
  it.each([
    ['order', ['project', 5]],
    ['product', ['product', 7]],
    ['customer', ['customer', 2]],
  ] as const)('given an id, %s removes the deleted row entry rather than refetching it', (kind, detail) => {
    const qc = new QueryClient();
    const key: unknown[] = [...detail];
    seed(qc, [['projects'], ['products'], ['customers'], key]);

    invalidateAfterDelete(qc, kind, detail[1]);

    expect(qc.getQueryState(key)).toBeUndefined();
  });

  it('leaves a NEIGHBOUR of the deleted row alone', () => {
    // `exact: true`: `['project', 5]` must not take `['project-archives', 5]`
    // or another order's entry with it.
    const qc = new QueryClient();
    seed(qc, [['project', 5], ['project', 6], ['project-archives', 5]]);

    invalidateAfterDelete(qc, 'order', 5);

    expect(qc.getQueryState(['project', 5])).toBeUndefined();
    expect(qc.getQueryState(['project', 6])).toBeDefined();
    expect(qc.getQueryState(['project-archives', 5])).toBeDefined();
  });

  it('without an id it leaves the detail entry in place, for the detail pages', () => {
    const qc = new QueryClient();
    seed(qc, [['projects'], ['project', 5]]);

    invalidateAfterDelete(qc, 'order');

    expect(qc.getQueryState(['project', 5])).toBeDefined();
    expect(stale(qc, ['project', 5])).toBe(false);
  });
});
