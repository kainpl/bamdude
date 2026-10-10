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
 * prefix costs almost nothing off the pages that read it — TanStack refetches
 * only queries that are currently *active*, and these six are mounted nowhere
 * else, with ONE exception: the sidebar's `['projects', 'nav-badges']` sits
 * under the `projects` prefix on every page, so each of these refreshes it too.
 * That is one COUNT, and it is what keeps the badge current.
 *
 * ⚠️ **On an order page it is NOT free**, and one key needs care because of
 * it: `project-plan` carries the operator's unsaved counts, so `PlanBlock`
 * reseeds on the plan's CONTENT rather than on the fact of a refetch. A key
 * added to this list that holds unsaved edits needs the same treatment.
 *
 * ⚠️ **The product keys are here because STOCK moves with an order** (pass 8,
 * Ruling 29). A line reserves kits off a product's shelf and a deleted line, a
 * cancelled order or a deleted order puts them back; a completed order-less
 * print credits one. Six call sites released stock and invalidated none of it,
 * so «Вільний залишок» and the catalog cards kept the pre-release numbers until
 * the page was reloaded — the classic "right after F5, wrong before it". The
 * per-product scoping is given up deliberately: the helper's whole job is that
 * a call site does not get to have an opinion about the list, and a prefix
 * costs nothing off a page that is not mounted.
 */

import type { QueryClient } from '@tanstack/react-query';

/**
 * The caches an order mutation can move, as key prefixes.
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
  // spec workshop-order-stage: every order mutation writes the order journal,
  // so the activity feed is an order view like the rest.
  'project-timeline',
  // spec workshop-order-queue: the order's queue section — a plan enqueue, a
  // line change or a status change moves what the order has waiting.
  'project-queue',
  'order-forecast', 'orders-forecast', // spec 2026-09-06: the ETA moves with the plan
  'order-filament', 'orders-filament', // spec 2026-09-07: the need moves with the plan
  'customers', // the customer tiles are computed from these orders
  'customer', // and one customer's page with them — the prefix, see above
  // WS-13 E13: the pickers read the customers and a customer's contacts through their own
  // projections; a customer or contact saved moves them as it moves the directory.
  'customer-options',
  'customer-contact-options',
  // pass 7: the orders a print dialog offers, and how many prints each still
  // needs. It IS an order view — the number comes from the plan engine — and it
  // is mounted only while such a dialog is open, so the prefix costs nothing
  // off that dialog. See `invalidateOrderCandidates` for the narrow call.
  'order-candidates',
  // pass 8: the free stock an order's lines take off and put back — the shelf
  // section, the product page's own `kits_available`, and the catalog cards
  // that show it. Prefixes, so an order that moved another product's shelf
  // (two lines, two products) is covered by one call. See the header.
  'product-stock',
  'product',
  'products',
  // spec workshop-add-to-order: the free kits of ONE configuration (the line
  // editor's ceiling) move with every reservation a line takes or hands back.
  'product-kits',
  // stock tab (2026-09-10): the farm-wide shelf and its journal move with the
  // same mutations that move a product's shelf — a reservation, a release, a
  // bank, an order deleted — and with the print completions the socket
  // reports. Prefixes: the page keys its queries by its filters.
  'stock-summary',
  // WS-13 E1 ST2: the journal's product filter moves with the journal.
  'stock-journal-products',
  // finished goods (WS-09): the journal of both ledgers shows the parts rows
  // these mutations write — every journal reads it in numbered pages now (WS-13
  // E12 E01) — and «can assemble» on the positions, their page and a dialog's
  // lookup reads the same free shelf.
  'stock-journal-page',
  'stock-items',
  'stock-item',
  'stock-lookup',
  // spec workshop-order-issue: the issue dialog's numbers and the «take from stock»
  // offers move with every assemble, receipt, issue and take; so do the dispatch
  // notes — the lists and the document (spec workshop-dispatch-notes).
  'project-fulfilment',
  'project-stock-offers',
  'dispatch-notes',
  'dispatch-note',
] as const;

/**
 * Mark the QUEUE views stale after a mutation that moved queued work.
 *
 * ⚠️ **`queue-forecast` is the reason this exists.** The queue page's
 * «estimated remaining» tile is a server-side simulation over exactly the rows
 * these mutations add, remove and reorder (spec 2026-09-06, Decision 5), and
 * it was invalidated only by the two WebSocket print events and a 30 s
 * interval. Queue a plate and the tile kept the old number until the interval
 * came round — the classic "right after F5, wrong before it", and here it is
 * the one figure the page exists to show.
 *
 * ⚠️ **`queue` is a PREFIX and the sweep is deliberately wide.** A queue
 * mutation on one printer moves the farm's makespan, which is every printer's
 * business; scoping this to the printer that was touched would leave the tile
 * and the other cards behind. TanStack refetches only ACTIVE queries, so off
 * the queue page it costs nothing, and on it the cost is one extra refetch per
 * mutation — priced and accepted (ruling 2026-09-07, final review I3).
 */
export function invalidateQueueViews(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ['queues'] });
  qc.invalidateQueries({ queryKey: ['queue'] });
  qc.invalidateQueries({ queryKey: ['auto-queue', 'summary'] });
  qc.invalidateQueries({ queryKey: ['queue-forecast'] });
  // An order's queue section lists these very rows (spec workshop-order-queue);
  // the order's figures follow the section when its rows change (OrderQueue).
  qc.invalidateQueries({ queryKey: ['project-queue'] });
}

/** A spool was written, used or synced: the shelf moved, and with it every «need vs shelf» figure. */
export function invalidateSpoolViews(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ['spools'] });
  qc.invalidateQueries({ queryKey: ['order-filament'] });
  qc.invalidateQueries({ queryKey: ['orders-filament'] });
}

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

/**
 * Mark the print dialogs' order proposal stale, and nothing else.
 *
 * ⚠️ **For a queue write that is not an order mutation** — `PrintModal`
 * queueing a library file. Its `outstanding_prints` is what the picker shows
 * ("still needs 5 prints"), the hook caches it for 30 s, and a second print of
 * the same file inside that window would otherwise be offered the count from
 * before the first. The dialog has no business invalidating the order PAGES,
 * which it may not even be filing under — hence one key rather than
 * `invalidateOrderViews`.
 */
export function invalidateOrderCandidates(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ['order-candidates'] });
}

/**
 * Every cache a stock movement can move (spec workshop-finished-goods, rule 26).
 *
 * A finished-goods move changes the positions, their tiles, the journal and
 * the sidebar badge; an assembly also takes parts off the free shelf, so the
 * parts list, the product page's shelf, its kits and the catalog cards move
 * with it. One list,
 * every prefix — a dialog does not get to have an opinion about which views it
 * touched, and TanStack refetches only the mounted ones.
 */
export const STOCK_KEYS: readonly (readonly string[])[] = [
  ['stock-items'],
  ['stock-item'],
  ['stock-lookup'],
  // Every journal — the tab, a position's, a product's — in numbered pages (WS-13 E12 E01).
  ['stock-journal-page'],
  ['stock-journal-products'],
  ['stock-summary'],
  ['stock-movements'],
  ['product-stock'],
  ['product-kits'],
  ['product'],
  ['products'],
  ['projects', 'nav-badges'],
  // A manual issue makes a dispatch note (spec workshop-dispatch-notes; final review I1).
  ['dispatch-notes'],
  ['dispatch-note'],
  // What an order may still take from the shelf and what it can issue are read off the
  // same shelves (WS-13 E13 F02).
  ['project-stock-offers'],
  ['project-fulfilment'],
];

export function invalidateStock(qc: QueryClient): void {
  for (const key of STOCK_KEYS) qc.invalidateQueries({ queryKey: [...key] });
}

type DeletedKind = 'order' | 'product' | 'customer';

/** The list keys each kind of deletion leaves behind. */
const DELETE_KEYS: Record<DeletedKind, readonly string[]> = {
  // The customer survives the order and their totals move with it; `customer`
  // is the prefix because the page that deleted it need not be the customer's.
  // The product keys are here for the same reason they are in
  // `ORDER_VIEW_KEYS` (Ruling 29): deleting an order releases every line's
  // reservation and re-credits its finished prints, so the shelf moves — and
  // the page that deleted it is usually a LIST, which knows no product at all.
  // The dispatch notes keep their text but lose a link to what was deleted (all three kinds).
  // The released reservations move the finished-goods shelves and the filament need of the
  // active orders too (WS-13 E13 F01).
  order: [
    'projects',
    'customers',
    'customer',
    'product-stock',
    'product',
    'products',
    'dispatch-notes',
    'dispatch-note',
    'orders-filament',
    'stock-items',
    'stock-item',
    'stock-summary',
    'stock-journal-page',
    'stock-lookup',
    'product-kits',
  ],
  // An order card renders the product's cover off the `projects` query; the
  // catalog's filter choices and category counts lose the product too.
  product: ['products', 'projects', 'product-facets', 'product-categories', 'dispatch-notes', 'dispatch-note'],
  // The orders survive their customer and lose the denormalised name.
  customer: ['customers', 'projects', 'dispatch-notes', 'dispatch-note'],
};

/**
 * The catalog's server figures (spec workshop-product-catalog) — the list with
 * its category panel, the sidebar's drafts badge, the filter choices and the
 * directory's per-category counts. Every mutation that creates a product or
 * changes its files calls this, never `['products']` alone: a duplicate always
 * makes a new draft, and a newly linked file can bring a material the filter did
 * not offer. `__tests__/utils/productCatalogInvalidation.test.ts` scans for the
 * call sites.
 */
export const PRODUCT_CATALOG_KEYS: readonly (readonly string[])[] = [
  ['products'],
  ['projects', 'nav-badges'],
  ['product-facets'],
  ['product-categories'],
  // The stock dialogs' own projection of the catalog (WS-13 E13 STK-10): a name, an option.
  ['stock-catalog'],
  ['stock-product'],
];

export function invalidateProductCatalog(qc: QueryClient): void {
  for (const queryKey of PRODUCT_CATALOG_KEYS) qc.invalidateQueries({ queryKey: [...queryKey] });
}

/**
 * What a product prints from, as key prefixes (WS-13 E1 CL3 / CL4): its plates, its
 * parts' sources, its files tab and its estimate — every one read off the product's
 * plates and the files behind them.
 *
 * ⚠️ `product-file-groups` is the files tab's DTO — the product's linked files with their
 * plates and its linked folders (WS-13 E9 A03); nothing reads the library's own
 * `LibraryFile[]` for a product any more.
 */
export const PRODUCT_FILE_KEYS = ['product-plates', 'product-part-sources', 'product-file-groups', 'product-estimate'] as const;

/**
 * Every view that shows a part's picture (spec part-thumbnails §12.2–12.3), as key prefixes: the
 * catalog and the product, its plates and files, the parts list, the stock section and the part
 * editor's candidates. The order views ride `ORDER_VIEW_KEYS` beside it. One server event
 * (`part_images_changed`) refreshes them for EVERY client — a render that finished, a choice
 * somebody else made, a merge, a link (spec §12.3).
 */
export const PART_IMAGE_VIEW_KEYS: readonly (readonly string[])[] = [
  ...PRODUCT_CATALOG_KEYS,
  ...STOCK_KEYS,
  ...PRODUCT_FILE_KEYS.map((key) => [key]),
  ['product-parts'],
  ['part-image-candidates'],
];

/**
 * Mark what a product prints from stale — after a composition, alias or merge edit,
 * a file linked or unlinked, a card re-read, a purchased part's price. Without a
 * product id every product's: a file trashed or restored in the file manager moves
 * the plates of whichever products it belongs to, and the page does not know which.
 */
export function invalidateProductFiles(qc: QueryClient, productId?: number): void {
  for (const key of PRODUCT_FILE_KEYS) {
    qc.invalidateQueries({ queryKey: productId == null ? [key] : [key, productId] });
  }
}

/**
 * After a product's variants changed (WS-13 E1 CL4): the product and the catalog's
 * figures, and — because a new group writes a choice into every order line and
 * stock position of the product — every order view and every stock view, and the
 * estimate of one standard unit.
 */
export function invalidateProductVariants(qc: QueryClient, productId: number): void {
  qc.invalidateQueries({ queryKey: ['product', productId] });
  invalidateProductCatalog(qc);
  invalidateOrderViews(qc);
  invalidateStock(qc);
  qc.invalidateQueries({ queryKey: ['product-estimate', productId] });
}

/** The DETAIL key of one deleted row — an order's page is `['project', id]`. */
const DETAIL_KEY: Record<DeletedKind, string> = {
  order: 'project',
  product: 'product',
  customer: 'customer',
};

/**
 * Mark the LISTS stale after a delete, and REMOVE the deleted row's own entry.
 *
 * ⚠️ **Removed, never invalidated.** Marking the deleted row's key stale asks
 * TanStack to refetch something that no longer exists while the page is still
 * mounted, which lands a 404 in the query and can flash the error state over a
 * page that is already on its way out. `removeQueries` drops the entry instead:
 * nothing is fetched and nothing is left to be read back.
 *
 * ⚠️ **The `id` is for LIST pages, and the three detail pages pass none.** From
 * a list, the deleted row's detail entry is a stale record sitting in a cache
 * nobody is watching, and the 60 s `staleTime` means the next click on a REUSED
 * id — or a Back into a route that no longer exists — renders it from cache
 * before any request goes out. From the row's OWN page the removal is
 * `useForgetOnUnmount`'s job instead, run on unmount so the query is not pulled
 * out from under the component still rendering it. Passing the id there would
 * blank the page mid-navigation; passing none from a list leaves the ghost.
 * That is why the parameter is optional rather than always required.
 */
export function invalidateAfterDelete(qc: QueryClient, kind: DeletedKind, id?: number): void {
  for (const key of DELETE_KEYS[kind]) {
    qc.invalidateQueries({ queryKey: [key] });
  }
  if (id !== undefined) {
    qc.removeQueries({ queryKey: [DETAIL_KEY[kind], id], exact: true });
  }
}
