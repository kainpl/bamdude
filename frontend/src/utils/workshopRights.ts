import type { Permission } from '../api/client';

/**
 * What a page outside the Workshop may change of the Workshop's (WS-13 E13 B06) — the
 * UI's copy of the server's gates (B01, B03), which stay the final word. A library
 * file or folder belongs to products and a print to an order: changing either is the
 * Workshop's business, so beside the right on the thing itself it needs the Workshop's
 * own: `products:update` for a product link, `orders:update` for an order.
 */

type HasPermission = (permission: Permission) => boolean;
type CanModify = (
  resource: 'queue' | 'archives' | 'library',
  action: 'update' | 'delete' | 'reprint',
  createdById: number | null | undefined,
) => boolean;

/** Link a library file to products, or unlink it. */
export function canLinkFile(hasPermission: HasPermission, canModify: CanModify, createdById: number | null | undefined) {
  return hasPermission('products:update') && canModify('library', 'update', createdById);
}

/** Link a folder to products, or unlink it — folders carry no owner, so the library side is `update_all`. */
export function canLinkFolder(hasPermission: HasPermission) {
  return hasPermission('products:update') && hasPermission('library:update_all');
}

/**
 * F(print) — file a past print under an order or take it out (WS-13 E13 O21): the Workshop's
 * own `orders:file_prints` files ANY print — a print from the printer's screen has no owner;
 * otherwise `orders:update` with the archive's own right (`update_own` only the caller's own,
 * an ownerless print only `update_all`).
 */
export function canFileArchive(hasPermission: HasPermission, canModify: CanModify, createdById: number | null | undefined) {
  return (
    hasPermission('orders:file_prints') ||
    (hasPermission('orders:update') && canModify('archives', 'update', createdById))
  );
}

/**
 * Fф — file work this request CREATES under an order (WS-13 E13 O21): `orders:file_prints` or
 * `orders:update`. The work is nobody's yet, so no archive right is asked. Every door of new
 * work asks it: the print dialog's order, a reprint, a clone, a plate's «Repeat».
 */
export function canFileFuturePrint(hasPermission: HasPermission) {
  return hasPermission('orders:file_prints') || hasPermission('orders:update');
}

/**
 * Taking from the shelf for an order — explicit numbers, `auto`, «Take from stock» (WS-13 E13
 * O06, O23): `stock:move`. Without it a line is added taking nothing (`stock: none`).
 */
export function canTakeStock(hasPermission: HasPermission) {
  return hasPermission('stock:move');
}

/** The shelf's figures — offers, proposals, a line's free kits — are the stock's read. */
export function canReadStock(hasPermission: HasPermission) {
  return hasPermission('stock:read');
}

/**
 * The product's own facts — its catalog, its detail, whether a choice of its variant values can be
 * ordered (`POST /products/{id}/combination`, owner 2026-11-14) — are the products' read.
 */
export function canReadProducts(hasPermission: HasPermission) {
  return hasPermission('products:read');
}

/**
 * Issuing an order's goods, receiving its prints, banking its surplus: `orders:update` +
 * `stock:move` (O06). Completing an order whose goods are out is neither — a consequence of the
 * order's own right (O23).
 */
export function canIssue(hasPermission: HasPermission) {
  return hasPermission('orders:update') && hasPermission('stock:move');
}

/** A write-off in the issue dialog corrects the books: issuing + `stock:adjust` (O06). */
export function canWriteOff(hasPermission: HasPermission) {
  return canIssue(hasPermission) && hasPermission('stock:adjust');
}

/**
 * Does a copy of work under an order — a reprint, a clone, a plate's «Repeat» — keep the order?
 * Only for whoever may file work under it, and only while it is open; anybody else gets the
 * explicit «without order» before sending (WS-13 E13 R11, O10). Nothing to inherit keeps nothing.
 */
export function keepsOrderOnCopy(projectId: number | null | undefined, hasPermission: HasPermission, open = true) {
  return projectId == null || (open && canFileFuturePrint(hasPermission));
}

/**
 * Would moving these files into a folder with these products change which products
 * any of them belongs to? A move REPLACES a file's products with the folder's — the
 * root has none — so such a move needs `products:update` too (B01).
 */
export function moveChangesProducts(files: { product_ids: number[] }[], folderProductIds: number[]) {
  const target = new Set(folderProductIds);
  return files.some((file) => {
    const own = new Set(file.product_ids);
    return own.size !== target.size || [...own].some((id) => !target.has(id));
  });
}
