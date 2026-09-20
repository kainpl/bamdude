import type { ProductAttachment } from '../../api/client';

/**
 * The order a category's attachments are shown in — ONE comparator, shared.
 *
 * ⚠️ **Both keys, always.** `sort_order` is what the operator sets with ↑ / ↓,
 * and a TIE in it is ordinary rather than a corner case: every upload into an
 * empty category starts at 0, and a reorder that names only some entries leaves
 * the rest sharing a rank. The server settles such a tie on the stored
 * `filename` (`product_files.sorted_attachments`), so anything that stops at
 * `sort_order` renders in whatever order the array happened to arrive in —
 * which is how the gallery could star one picture while `/cover-image` served
 * another.
 *
 * The stored filename is a uuid, not the designer's name: it is meaningless to
 * read but it is STABLE, and stability is the whole job of a tie-break. Sorting
 * on `original_name` instead would look tidier and disagree with the server.
 */
export const byAttachmentOrder = (a: ProductAttachment, b: ProductAttachment) =>
  a.sort_order - b.sort_order || a.filename.localeCompare(b.filename);
