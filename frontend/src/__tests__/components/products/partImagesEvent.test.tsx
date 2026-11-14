/**
 * Spec part-thumbnails §12.3, §15: a page showing «being prepared» leaves it when the server says a
 * picture changed — the event's keys reach the product page's query, and nothing polls.
 */
import { describe, expect, it, vi } from 'vitest';
import { act, screen } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Product, ProductPart } from '../../../api/client';
import { CompositionTab } from '../../../components/products/detail/CompositionTab';
import { PART_IMAGE_VIEW_KEYS } from '../../../utils/queryInvalidation';

function lid(image: ProductPart['image']): ProductPart {
  return {
    id: 2,
    kind: 'printed',
    name: 'Lid',
    name_key: 'lid',
    qty_per_unit: 1,
    aliases: [],
    auto: false,
    unit_price: null,
    sourcing_url: null,
    remarks: null,
    sort_order: 2,
    ignored: false,
    stock_balance: 0,
    variant_option_ids: [],
    image,
  };
}

const product = (parts: ProductPart[]) =>
  ({ id: 7, code: 'PR-0007', name: 'Flask', parts, variant_groups: [] }) as unknown as Product;

// The test's handle on the page's client — set in an effect, as `ProductJournal.test.tsx` does.
const handle = vi.hoisted(() => ({ client: null as QueryClient | null }));

function Host() {
  const qc = useQueryClient();
  useEffect(() => {
    handle.client = qc;
  }, [qc]);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const query = useQuery({ queryKey: ['product', 7], queryFn: () => api.getProduct(7) });
  return query.data ? <CompositionTab product={query.data} headingRef={headingRef} /> : null;
}

describe('part_images_changed', () => {
  it('takes a part out of «being prepared» once its render is ready', async () => {
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ currency: 'EUR' } as never);
    vi.spyOn(api, 'getProductSources').mockResolvedValue({ parts: [] } as never);
    vi.spyOn(api, 'getProduct')
      .mockResolvedValueOnce(product([lid({ kind: 'render', status: 'pending', v: null })]))
      .mockResolvedValueOnce(product([lid({ kind: 'render', status: 'ready', v: 'r2' })]));
    render(<Host />);
    expect(await screen.findByRole('img', { name: 'Lid: the picture is being prepared' })).toBeInTheDocument();
    // What the socket handler does for the event (E4: `debouncedInvalidate(...PART_IMAGE_VIEW_KEYS)`).
    const client = handle.client as QueryClient;
    await act(async () => {
      for (const key of PART_IMAGE_VIEW_KEYS) await client.invalidateQueries({ queryKey: [...key] });
    });
    expect(await screen.findByRole('button', { name: 'Show Lid larger' })).toBeInTheDocument();
  });
});
