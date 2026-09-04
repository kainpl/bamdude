/**
 * One comparator, two components. The gallery and the attachments list render
 * different categories of the SAME column, and they used to sort it by
 * different rules — the gallery broke a `sort_order` tie on `filename` (which
 * is what the server does), the attachments list left the tie to whatever order
 * the array arrived in. A tie is ordinary: every upload into an empty category
 * starts at 0, and a reorder that names only some entries leaves the rest
 * sharing a rank.
 */

import { describe, it, expect } from 'vitest';
import type { ProductAttachment } from '../../../api/client';
import { byAttachmentOrder } from '../../../components/products/attachmentOrder';

const entry = (filename: string, sort_order: number): ProductAttachment =>
  ({ filename, sort_order, category: 'pictures' }) as ProductAttachment;

describe('byAttachmentOrder', () => {
  it('orders by sort_order first', () => {
    const sorted = [entry('z.png', 0), entry('a.png', 2), entry('m.png', 1)].sort(byAttachmentOrder);
    expect(sorted.map((a) => a.filename)).toEqual(['z.png', 'm.png', 'a.png']);
  });

  it('breaks a tie on the stored filename, the way the server does', () => {
    const sorted = [entry('z.png', 0), entry('a.png', 0)].sort(byAttachmentOrder);
    expect(sorted.map((a) => a.filename)).toEqual(['a.png', 'z.png']);
  });
});
