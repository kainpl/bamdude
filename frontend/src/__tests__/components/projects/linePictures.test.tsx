/**
 * Spec part-thumbnails §12.2, plan E6 (task 43): an order line's parts with their pictures — the line's
 * parts table (printed and bought), the parts list of a «parts only» line in the line editor, the
 * configuration dialog's kit and the part a change drops, whose picture is the live product's (D5).
 */
import { useEffect } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { LinePurchasedPart, PartFigures, PartImageRef, Product, ProjectLine } from '../../../api/client';
import { LineConfigDialog } from '../../../components/projects/LineConfigDialog';
import { LineEditDialog } from '../../../components/projects/LineEditDialog';
import { LinePartsTable } from '../../../components/projects/LinePartsTable';
import { invalidatePartImageViews } from '../../../utils/queryInvalidation';
import { makeLine, makeOrder } from '../../fixtures/orderDetail';

// The test's handle on the dialog's client — set in an effect, as `partImagesEvent.test.tsx` does (E5).
const handle = vi.hoisted(() => ({ client: null as QueryClient | null }));

const ready = (v: string): PartImageRef => ({ kind: 'render', status: 'ready', v });
const src = (id: number, v: string) => `/api/v1/product-parts/${id}/image?v=${v}&size=sm`;

const figures = (over: Partial<PartFigures>): PartFigures => ({
  part_id: 1,
  name: 'shade',
  qty_per_unit: 1,
  need: 4,
  usable: 2,
  in_progress: 0,
  remaining: 2,
  surplus: 0,
  variant: false,
  queued: 0,
  bankable: 0,
  ...over,
});

describe('an order line’s parts table', () => {
  it('a printed part shows its picture; a bought one its photo, or the bought glyph', () => {
    const screw: LinePurchasedPart = {
      part_id: 3,
      name: 'M3 screw',
      per: 4,
      need: 16,
      variant: false,
      image: { kind: 'photo', status: 'ready', v: 's3.jpg' },
    };
    const nut: LinePurchasedPart = { part_id: 4, name: 'M3 nut', per: 4, need: 16, variant: false, image: null };
    render(<LinePartsTable parts={[figures({ image: ready('p1') })]} purchased={[screw, nut]} mode="product" />);
    const rowOf = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;
    expect(within(rowOf('shade')).getByTestId('part-thumb-img')).toHaveAttribute('src', src(1, 'p1'));
    expect(within(rowOf('M3 screw')).getByTestId('part-thumb-img')).toHaveAttribute('src', src(3, 's3.jpg'));
    expect(within(rowOf('M3 nut')).getByTestId('part-thumb').querySelector('[data-glyph="purchased"]')).not.toBeNull();
    // a picture adds no text: the row reads as it did
    expect(
      within(rowOf('shade'))
        .getAllByRole('cell')
        .map((td) => td.textContent),
    ).toEqual(['shade', '× 1', '4', '2', '0 / 0', '2', '0']);
  });
});

describe('a «parts only» line in the line editor', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue({ id: 2, name: 'Lid', materials: [], colors: [] } as never);
    vi.spyOn(api, 'getProductStock').mockResolvedValue({ kits_available: 0, balances: [], movements: [] } as never);
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [] } as never);
  });

  it('lists each part with its picture', async () => {
    const line = makeLine({
      id: 11,
      product_id: 2,
      product_name: 'Lid',
      mode: 'parts',
      quantity: 1,
      parts: [figures({ part_id: 1, name: 'flask', qty_per_unit: 2, image: ready('f1') })],
    });
    render(<LineEditDialog order={makeOrder({ lines: [line] })} line={line} onClose={() => {}} onConfigure={() => {}} />);
    const dialog = await screen.findByRole('dialog', { name: 'Edit line' });
    const item = within(dialog).getByText('flask × 2');
    expect(within(item).getByTestId('part-thumb-img')).toHaveAttribute('src', src(1, 'f1'));
  });
});

const product = {
  id: 7,
  name: 'Pipe',
  parts: [
    { id: 5, kind: 'printed', name: 'Колба', qty_per_unit: 1, sort_order: 0, variant_option_ids: [], image: ready('k5') },
    { id: 6, kind: 'printed', name: 'straight tail', qty_per_unit: 1, sort_order: 1, variant_option_ids: [11], image: ready('t6') },
    {
      id: 8,
      kind: 'printed',
      name: 'angled tail',
      qty_per_unit: 1,
      sort_order: 2,
      variant_option_ids: [12],
      image: { kind: 'render', status: 'pending', v: null },
    },
    { id: 10, kind: 'purchased', name: 'screw', qty_per_unit: 2, sort_order: 3, variant_option_ids: [], image: null },
  ],
  variant_groups: [
    {
      id: 3,
      name: 'Хвіст',
      position: 0,
      default_option_id: 11,
      options: [
        { id: 11, name: 'прямий', position: 0 },
        { id: 12, name: 'кутовий', position: 1 },
      ],
    },
  ],
} as unknown as Product;

const configLine = {
  id: 21,
  product_id: 7,
  product_name: 'Pipe',
  quantity: 4,
  mode: 'product',
  config_key: '3=11',
  configuration: {
    choices: [{ group_id: 3, group_name: 'Хвіст', option_id: 11, option_name: 'прямий', is_default: true }],
    changed_parts: [],
  },
  parts: [],
} as unknown as ProjectLine;

const noImpact = { reserved_before: 0, reserved_after: 0, finished_before: 0, finished_after: 0, dropping: [] };

describe('the line’s configuration dialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProduct').mockResolvedValue(product);
    vi.spyOn(api, 'getProductSources').mockResolvedValue({ parts: [] } as never);
  });

  it('each part of the kit shows its picture; a queued one says so; a bought one the bought glyph', async () => {
    vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue(noImpact);
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={configLine} onClose={() => {}} />);
    const row = await screen.findByTestId('config-part-5');
    expect(within(row).getByTestId('part-thumb-img')).toHaveAttribute('src', src(5, 'k5'));
    expect(
      within(screen.getByTestId('config-part-8')).getByRole('img', { name: 'angled tail: the picture is being prepared' }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId('config-part-10')).getByTestId('part-thumb').querySelector('[data-glyph="purchased"]'),
    ).not.toBeNull();
  });

  it('a part the change drops shows the live product’s picture beside its sentence (plan E6, D5)', async () => {
    vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue({
      reserved_before: 4,
      reserved_after: 4,
      finished_before: 0,
      finished_after: 0,
      // the dry run's picture is older than the product's — the live one is shown
      dropping: [
        {
          part_id: 6,
          name: 'straight tail',
          per_before: 1,
          per_after: 0,
          printed: 3,
          queued: 1,
          image: { kind: 'render', status: 'pending', v: null },
        },
      ],
    });
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={configLine} onClose={() => {}} />);
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    const sentence = await screen.findByText('straight tail: 3 printed, 1 queued will become surplus');
    expect(within(sentence).getByTestId('part-thumb-img')).toHaveAttribute('src', src(6, 't6'));
  });

  it('a picture that changes reaches the dropped part through the product; the dry run is not asked again (plan E6, D5)', async () => {
    const preview = vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue({
      reserved_before: 4,
      reserved_after: 4,
      finished_before: 0,
      finished_after: 0,
      dropping: [{ part_id: 6, name: 'straight tail', per_before: 1, per_after: 0, printed: 3, queued: 1, image: null }],
    });
    const withTail = (image: PartImageRef) =>
      ({ ...product, parts: product.parts.map((p) => (p.id === 6 ? { ...p, image } : p)) }) as Product;
    const getProduct = vi.spyOn(api, 'getProduct').mockResolvedValue(withTail({ kind: 'render', status: 'pending', v: null }));
    render(<ConfigHost />);
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    const text = 'straight tail: 3 printed, 1 queued will become surplus';
    expect(
      within(await screen.findByText(text)).getByRole('img', { name: 'straight tail: the picture is being prepared' }),
    ).toBeInTheDocument();
    // What the socket handler does for `part_images_changed` (E4): the product is read again, the dry run is not.
    getProduct.mockResolvedValue(withTail(ready('t7')));
    await act(async () => {
      invalidatePartImageViews(handle.client as QueryClient);
    });
    await waitFor(() =>
      expect(within(screen.getByText(text)).getByTestId('part-thumb-img')).toHaveAttribute('src', src(6, 't7')),
    );
    expect(preview).toHaveBeenCalledTimes(1);
  });

  it('two dropped parts of one name are two lines, each its own part — keyed by the part, not the sentence (final review)', async () => {
    const twins = {
      ...product,
      parts: [...product.parts, { id: 11, kind: 'printed', name: 'straight tail', qty_per_unit: 1, sort_order: 5, variant_option_ids: [11], image: ready('s11') }],
    } as Product;
    vi.spyOn(api, 'getProduct').mockResolvedValue(twins);
    vi.spyOn(api, 'previewLineConfiguration').mockResolvedValue({
      reserved_before: 4,
      reserved_after: 4,
      finished_before: 0,
      finished_after: 0,
      dropping: [
        { part_id: 6, name: 'straight tail', per_before: 1, per_after: 0, printed: 3, queued: 1, image: null },
        { part_id: 11, name: 'straight tail', per_before: 1, per_after: 0, printed: 3, queued: 1, image: null },
      ],
    });
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<LineConfigDialog orderId={9} orderCode="OR-0009" line={configLine} onClose={() => {}} />);
    fireEvent.click(within(await screen.findByRole('group', { name: 'Хвіст' })).getByRole('radio', { name: 'кутовий' }));
    const lines = await screen.findAllByText('straight tail: 3 printed, 1 queued will become surplus');
    expect(lines.map((p) => within(p).getByTestId('part-thumb-img').getAttribute('src'))).toEqual([src(6, 't6'), src(11, 's11')]);
    expect(errors.mock.calls.filter(([message]) => String(message).includes('same key'))).toEqual([]);
  });
});

function ConfigHost() {
  const qc = useQueryClient();
  useEffect(() => {
    handle.client = qc;
  }, [qc]);
  return <LineConfigDialog orderId={9} orderCode="OR-0009" line={configLine} onClose={() => {}} />;
}
