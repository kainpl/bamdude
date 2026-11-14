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

import { describe, it, expect, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import {
  ORDER_VIEW_KEYS,
  PART_IMAGE_PURE_KEYS,
  PART_IMAGE_VIEW_KEYS,
  PRODUCT_CATALOG_KEYS,
  PRODUCT_COMBINATIONS_KEY,
  PRODUCT_FILE_KEYS,
  STOCK_KEYS,
  invalidateAfterDelete,
  invalidateOrderCandidates,
  invalidateOrderViews,
  invalidateProductFiles,
  invalidateProductVariants,
  invalidateQueueViews,
  invalidateSpoolViews,
  invalidateStock,
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
      ['order-printer-resources', 5],
    ]);

    invalidateOrderViews(qc);

    expect(stale(qc, ['project', 5])).toBe(true);
    expect(stale(qc, ['customer', 2])).toBe(true);
    expect(stale(qc, ['projects', {}])).toBe(true);
    expect(stale(qc, ['project-plan', 5])).toBe(true);
    expect(stale(qc, ['order-printer-resources', 5])).toBe(true);
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
    seed(qc, [['archives'], ['queue'], ['library-files']]);

    invalidateOrderViews(qc);

    expect(stale(qc, ['archives'])).toBe(false);
    expect(stale(qc, ['queue'])).toBe(false);
    expect(stale(qc, ['library-files'])).toBe(false);
  });

  it('moves the product views too, because stock moves with an order', () => {
    // Ruling 29. A line reserves kits off a product's shelf; deleting the line,
    // cancelling the order or deleting it puts them back. Six call sites did
    // that and invalidated none of it, so «Вільний залишок» and the catalog
    // cards kept the pre-release numbers until the page was reloaded. The
    // per-product scoping is given up on purpose — most of those call sites
    // know no product at all, and a prefix costs nothing off a page that is not
    // mounted.
    const qc = new QueryClient();
    seed(qc, [
      ['product-stock', 7],
      ['product', 7],
      ['products', {}],
    ]);

    invalidateOrderViews(qc);

    expect(stale(qc, ['product-stock', 7])).toBe(true);
    expect(stale(qc, ['product', 7])).toBe(true);
    expect(stale(qc, ['products', {}])).toBe(true);
  });

  it('publishes the keys as a list the websocket hook can walk', () => {
    // `useWebSocket` cannot call the helper: its invalidations are debounced
    // and staggered through one shared timer, so it needs the KEYS rather than
    // the calls. Exporting the list is what keeps the two in step.
    expect([...ORDER_VIEW_KEYS]).toEqual([
      'projects',
      'project',
      'order-printer-resources',
      'project-archives',
      'project-plan',
      'order-print-options',
      // spec workshop-order-stage: every order mutation writes the journal the feed shows.
      'project-timeline',
      // spec workshop-order-queue: the order's queue section moves with the plan and the lines.
      'project-queue',
      // spec 2026-09-06: the ETA is read off the plan, so it moves with it.
      'order-forecast',
      'orders-forecast',
      'order-draft-forecast',
      'order-draft-filament',
      // spec 2026-09-07: the need moves with the plan
      'order-filament',
      'orders-filament',
      'customers',
      'customer',
      // WS-13 E13: the pickers' own reads of the customers and a customer's contacts.
      'customer-options',
      'customer-contact-options',
      'order-candidates',
      'product-stock',
      'product',
      'products',
      // spec workshop-add-to-order: one configuration's free kits.
      'product-kits',
      // stock tab (2026-09-10)
      'stock-summary',
      // WS-13 E1 ST2: the journal's product filter.
      'stock-journal-products',
      // finished goods (WS-09): the journal shows the parts rows an order moves,
      // and «can assemble» reads the same free shelf — every journal paged (E12 E01).
      'stock-journal-page',
      'stock-items',
      'stock-item',
      'stock-lookup',
      'project-fulfilment',
      'project-stock-offers',
      'dispatch-notes',
      'dispatch-note',
    ]);
  });

  it('a catalog edit refreshes the stock dialogs’ own projection of the catalog (WS-13 E13 STK-10)', () => {
    expect(PRODUCT_CATALOG_KEYS).toEqual(expect.arrayContaining([['stock-catalog'], ['stock-product']]));
  });

  it('a stock movement refreshes the catalog cards and the sidebar badge too', () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, 'invalidateQueries');
    invalidateStock(qc);
    const keys = spy.mock.calls.map(([filters]) => JSON.stringify(filters?.queryKey));
    expect(keys).toEqual(expect.arrayContaining(['["products"]', '["projects","nav-badges"]', '["stock-journal-page"]']));
  });

  it('pins the stock keys — a new view of the shelves is added here, on purpose (E13 F04)', () => {
    expect(STOCK_KEYS.map((key) => key.join(' '))).toEqual([
      'stock-items',
      'stock-item',
      'stock-lookup',
      'stock-journal-page',
      'stock-journal-products',
      'stock-summary',
      'stock-movements',
      'product-stock',
      'product-kits',
      'product',
      'products',
      'projects nav-badges',
      'dispatch-notes',
      'dispatch-note',
      'project-stock-offers',
      'project-fulfilment',
    ]);
  });

  it('a stock movement moves an order’s take-from-stock offers and its issue state (E13 F02)', () => {
    const qc = new QueryClient();
    seed(qc, [['project-stock-offers', 5], ['project-fulfilment', 5]]);
    invalidateStock(qc);
    expect(stale(qc, ['project-stock-offers', 5])).toBe(true);
    expect(stale(qc, ['project-fulfilment', 5])).toBe(true);
  });

  it('a manual issue refreshes the dispatch notes it made (final review I1)', () => {
    const qc = new QueryClient();
    seed(qc, [['dispatch-notes', { page: 1, sort_by: 'created-desc' }], ['dispatch-note', 7]]);
    invalidateStock(qc);
    expect(stale(qc, ['dispatch-notes', { page: 1, sort_by: 'created-desc' }])).toBe(true);
    expect(stale(qc, ['dispatch-note', 7])).toBe(true);
  });
});

describe('invalidateQueueViews', () => {
  it('moves the farm estimate with the queue rows it is computed from', () => {
    // ⚠️ The tile is a server-side simulation over exactly these rows (spec
    // 2026-09-06, Decision 5). It used to be refreshed only by the two
    // WebSocket print events and a 30 s interval, so queueing a plate left the
    // one figure the page exists to show a full interval behind.
    const qc = new QueryClient();
    seed(qc, [['queues'], ['queue', 3, 'pending'], ['queue', 'all', 'pending'], ['queue', 'summary'], ['auto-queue', 'summary'], ['queue-forecast'], ['projects']]);

    invalidateQueueViews(qc);

    expect(stale(qc, ['queues'])).toBe(true);
    // The prefix: a mutation on one printer moves the whole farm's makespan.
    expect(stale(qc, ['queue', 3, 'pending'])).toBe(true);
    expect(stale(qc, ['queue', 'all', 'pending'])).toBe(true);
    expect(stale(qc, ['queue', 'summary'])).toBe(true);
    expect(stale(qc, ['auto-queue', 'summary'])).toBe(true);
    expect(stale(qc, ['queue-forecast'])).toBe(true);
    // Not an order mutation — the order views are none of its business.
    expect(stale(qc, ['projects'])).toBe(false);
  });
  it('moves an order\'s queue section too — cancelling its job on the queue page is its business', () => {
    // spec workshop-order-queue: the section lists the rows this very mutation
    // changed; with a 60 s staleTime a Back to the order would draw the old row.
    const qc = new QueryClient();
    seed(qc, [['project-queue', 7], ['project', 7]]);

    invalidateQueueViews(qc);

    expect(stale(qc, ['project-queue', 7])).toBe(true);
    // The order's own figures follow the section (OrderQueue), not this sweep.
    expect(stale(qc, ['project', 7])).toBe(false);
  });
});

describe('invalidateSpoolViews', () => {
  it('invalidateSpoolViews marks the spools and both filament-needs views stale', () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, 'invalidateQueries');
    invalidateSpoolViews(qc);
    expect(spy.mock.calls.map((c) => c[0]?.queryKey)).toEqual([['spools'], ['order-filament'], ['orders-filament']]);
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
    seed(qc, [
      ['projects'],
      ['customers'],
      ['customer', 2],
      ['project', 5],
      ['product-stock', 7],
      ['product', 7],
      ['products'],
    ]);

    invalidateAfterDelete(qc, 'order');

    expect(stale(qc, ['projects'])).toBe(true);
    expect(stale(qc, ['customers'])).toBe(true);
    // The customer survives the order, so their page is stale, not gone.
    expect(stale(qc, ['customer', 2])).toBe(true);
    expect(stale(qc, ['project', 5])).toBe(false);
    // Deleting an order releases every line's reservation and re-credits its
    // finished prints (Rulings 25–26), so the shelf moved — and the page that
    // deleted it is usually a LIST, which knows no product to scope by.
    expect(stale(qc, ['product-stock', 7])).toBe(true);
    expect(stale(qc, ['product', 7])).toBe(true);
    expect(stale(qc, ['products'])).toBe(true);
  });

  it('an order delete releases its reservations: the filament need and the shelves move (E13 F01)', () => {
    const qc = new QueryClient();
    const keys = [
      ['orders-filament', { status: 'active' }],
      ['stock-items', { page: 1 }],
      ['stock-item', 3],
      ['stock-summary'],
      ['stock-journal-page', { page: 1 }],
      ['stock-lookup', 7, 'std'],
      ['product-kits', 7, 'std'],
    ];
    seed(qc, keys);
    invalidateAfterDelete(qc, 'order');
    for (const key of keys) expect(stale(qc, key)).toBe(true);
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

  it('refreshes the dispatch notes whose links a deletion leaves dangling (final review I1)', () => {
    for (const kind of ['order', 'customer', 'product'] as const) {
      const qc = new QueryClient();
      seed(qc, [['dispatch-notes', { page: 1 }], ['dispatch-note', 7]]);
      invalidateAfterDelete(qc, kind);
      expect(stale(qc, ['dispatch-notes', { page: 1 }])).toBe(true);
      expect(stale(qc, ['dispatch-note', 7])).toBe(true);
    }
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

describe('invalidateProductFiles (WS-13 E1 CL3 / CL4)', () => {
  it('marks what a product prints from stale — not the linked-files list, another DTO under its own key', () => {
    const qc = new QueryClient();
    seed(qc, [
      ['product-plates', 7],
      ['product-part-sources', 7],
      ['product-file-groups', 7],
      ['product-estimate', 7],
      ['product-files', 7],
      ['product-plates', 8],
    ]);
    invalidateProductFiles(qc, 7);
    for (const key of PRODUCT_FILE_KEYS) expect(stale(qc, [key, 7])).toBe(true);
    expect(PRODUCT_FILE_KEYS).toContain('product-file-groups');
    expect(PRODUCT_FILE_KEYS).not.toContain('product-files');
    expect(stale(qc, ['product-files', 7])).toBe(false);
    expect(stale(qc, ['product-plates', 8])).toBe(false);
  });

  it('without a product touches every product — the trash hides a file from all of them', () => {
    const qc = new QueryClient();
    seed(qc, [['product-plates', 7], ['product-estimate', 8]]);
    invalidateProductFiles(qc);
    expect(stale(qc, ['product-plates', 7])).toBe(true);
    expect(stale(qc, ['product-estimate', 8])).toBe(true);
  });

  it('takes the combinations table with it — a part, a link or a slice opens or closes one', () => {
    const qc = new QueryClient();
    seed(qc, [
      [PRODUCT_COMBINATIONS_KEY, 7, 1],
      [PRODUCT_COMBINATIONS_KEY, 7, 2],
      [PRODUCT_COMBINATIONS_KEY, 8, 1],
    ]);
    invalidateProductFiles(qc, 7);
    expect(stale(qc, [PRODUCT_COMBINATIONS_KEY, 7, 1])).toBe(true);
    expect(stale(qc, [PRODUCT_COMBINATIONS_KEY, 7, 2])).toBe(true);
    expect(stale(qc, [PRODUCT_COMBINATIONS_KEY, 8, 1])).toBe(false);
    invalidateProductFiles(qc);
    expect(stale(qc, [PRODUCT_COMBINATIONS_KEY, 8, 1])).toBe(true);
  });

  it('the combinations table is no picture view and no catalog figure', () => {
    for (const list of [PART_IMAGE_VIEW_KEYS, PART_IMAGE_PURE_KEYS, PRODUCT_CATALOG_KEYS]) {
      expect(list.some((key) => key[0] === PRODUCT_COMBINATIONS_KEY)).toBe(false);
    }
    expect((ORDER_VIEW_KEYS as readonly string[]).includes(PRODUCT_COMBINATIONS_KEY)).toBe(false);
  });
});

describe('invalidateProductVariants (WS-13 E1 CL4)', () => {
  it('moves the product, the catalog, every order view, the stock and the estimate', () => {
    // A new group writes a choice into every order line and stock position of the product.
    const qc = new QueryClient();
    seed(qc, [['product', 7], ['products'], ['projects'], ['project', 3], ['stock-items'], ['product-estimate', 7]]);
    invalidateProductVariants(qc, 7);
    for (const key of [['product', 7], ['products'], ['projects'], ['project', 3], ['stock-items'], ['product-estimate', 7]]) {
      expect(stale(qc, key)).toBe(true);
    }
  });

  it('moves the product’s combinations table, every page of it, and no other product’s', () => {
    const qc = new QueryClient();
    seed(qc, [
      [PRODUCT_COMBINATIONS_KEY, 7, 1],
      [PRODUCT_COMBINATIONS_KEY, 7, 3],
      [PRODUCT_COMBINATIONS_KEY, 8, 1],
    ]);
    invalidateProductVariants(qc, 7);
    expect(stale(qc, [PRODUCT_COMBINATIONS_KEY, 7, 1])).toBe(true);
    expect(stale(qc, [PRODUCT_COMBINATIONS_KEY, 7, 3])).toBe(true);
    expect(stale(qc, [PRODUCT_COMBINATIONS_KEY, 8, 1])).toBe(false);
  });
});

describe('the journal product filter', () => {
  it('moves with the journal', () => {
    const qc = new QueryClient();
    seed(qc, [['stock-journal-products', 'both']]);
    invalidateStock(qc);
    expect(stale(qc, ['stock-journal-products', 'both'])).toBe(true);
    expect(ORDER_VIEW_KEYS).toContain('stock-journal-products');
  });
});
