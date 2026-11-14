/**
 * The composition doors — a part created, edited, deleted or merged — drop the cached pictures of
 * THEIR product at once, without waiting for the socket (spec procurement-stock-images R6, A16):
 * SQLite hands a deleted part's id to the next part, and its old picture must not sit on it.
 */
import { describe, expect, it } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import type { PartImageMeta } from '../../../api/client';
import { invalidateComposition } from '../../../components/products/partMutations';
import { PART_IMAGE_REFS_KEY, PRODUCT_COMBINATIONS_KEY } from '../../../utils/queryInvalidation';

const meta = (part: number, product: number): PartImageMeta => ({
  part_id: part,
  product_id: product,
  image: { kind: 'photo', status: 'ready', v: `p${part}` },
});

describe('invalidateComposition', () => {
  it('drops the cached pictures of the product whose parts changed, and only those', () => {
    const qc = new QueryClient();
    qc.setQueryData([PART_IMAGE_REFS_KEY, 1], meta(1, 7));
    qc.setQueryData([PART_IMAGE_REFS_KEY, 2], meta(2, 8));
    invalidateComposition(qc, 7);
    expect(qc.getQueryState([PART_IMAGE_REFS_KEY, 1])).toBeUndefined();
    expect(qc.getQueryData([PART_IMAGE_REFS_KEY, 2])).toEqual(meta(2, 8));
    qc.clear();
  });

  it('a part written opens or closes combinations: the product’s table is read again', () => {
    const qc = new QueryClient();
    qc.setQueryData([PRODUCT_COMBINATIONS_KEY, 7, 1], { total: 0, page: 1, items: [] });
    qc.setQueryData([PRODUCT_COMBINATIONS_KEY, 8, 1], { total: 0, page: 1, items: [] });
    invalidateComposition(qc, 7);
    expect(qc.getQueryState([PRODUCT_COMBINATIONS_KEY, 7, 1])?.isInvalidated).toBe(true);
    expect(qc.getQueryState([PRODUCT_COMBINATIONS_KEY, 8, 1])?.isInvalidated).toBe(false);
    qc.clear();
  });
});
