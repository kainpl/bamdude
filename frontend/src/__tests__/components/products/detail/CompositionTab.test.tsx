/**
 * The «Composition» tab of the product page (WS-13 E9 D01–D05): the variants card, the
 * read tables of printed and purchased parts with the sources their plates give, and the
 * doors into the editors (WS-13 E10 C10) — «Add part» and a row's «Edit» open the part
 * dialog, «Merge into…» the merge, «Delete» the confirmation; «Manage variants…» the
 * variants dialog. The tab has no fields of its own.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useRef, type ReactElement } from 'react';
import { render } from '../../../utils';
import { api } from '../../../../api/client';
import type { Permission, Product, ProductCombinationsPage, ProductPart, ProductSources } from '../../../../api/client';
import { CompositionTab } from '../../../../components/products/detail/CompositionTab';

const auth = vi.hoisted(() => ({ granted: null as Set<string> | null }));
vi.mock('../../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => {
      const real = actual.useAuth();
      return { ...real, hasPermission: (p: Permission) => auth.granted?.has(p) ?? real.hasPermission(p) };
    },
  };
});

function part(over: Partial<ProductPart> & Pick<ProductPart, 'id' | 'name'>): ProductPart {
  return {
    kind: 'printed',
    name_key: over.name.toLowerCase(),
    qty_per_unit: 1,
    aliases: [],
    auto: false,
    unit_price: null,
    sourcing_url: null,
    remarks: null,
    sort_order: over.id,
    ignored: false,
    stock_balance: 0,
    variant_option_ids: [],
    ...over,
  };
}

const parts: ProductPart[] = [
  part({ id: 1, name: 'Body', auto: true, aliases: ['body_v2'] }),
  part({ id: 2, name: 'Lid', qty_per_unit: 0, variant_option_ids: [11] }),
  part({ id: 3, name: 'Cube', qty_per_unit: 0, ignored: true }),
  part({ id: 4, name: 'Magnet', kind: 'purchased', qty_per_unit: 4, unit_price: 0.5, sourcing_url: 'https://shop.example/m', remarks: 'N52' }),
  part({ id: 5, name: 'Screw', kind: 'purchased', qty_per_unit: 2 }),
];

const product = {
  id: 7,
  code: 'PR-0007',
  name: 'Flask',
  parts,
  parts_count: 5,
  variant_groups: [
    {
      id: 1,
      name: 'Lid type',
      position: 0,
      default_option_id: 10,
      lines_count: 0,
      stock_count: 0,
      parts_count: 1,
      options: [
        { id: 10, name: 'Glass', position: 0, lines_count: 0, parts_count: 0, stock_count: 0 },
        { id: 11, name: 'Cork', position: 1, lines_count: 0, parts_count: 1, stock_count: 0 },
      ],
    },
  ],
} as unknown as Product;

const sources: ProductSources = {
  parts: [
    {
      part_id: 1,
      has_sliced_source: true,
      yield_min: 1,
      yield_max: 2,
      hidden_sources: 1,
      sources: [
        {
          plate_id: 1,
          library_file_id: 30,
          filename: 'body.3mf',
          folder_id: null,
          folder_name: null,
          hidden: false,
          plate_index: 2,
          printer_model: 'X1C',
          sliced: true,
          yield: 2,
          print_time_seconds: 100,
          filament_used_grams: 5,
          recommended: true,
        },
        {
          plate_id: 2,
          library_file_id: 31,
          filename: null,
          folder_id: null,
          folder_name: null,
          hidden: true,
          plate_index: 1,
          printer_model: 'P1S',
          sliced: true,
          yield: 1,
          print_time_seconds: 100,
          filament_used_grams: 5,
          recommended: false,
        },
      ],
    },
    {
      part_id: 2,
      has_sliced_source: false,
      yield_min: null,
      yield_max: null,
      hidden_sources: 0,
      sources: [
        {
          plate_id: 3,
          library_file_id: 32,
          filename: 'lid.stl.3mf',
          folder_id: null,
          folder_name: null,
          hidden: false,
          plate_index: 1,
          printer_model: null,
          sliced: false,
          yield: 1,
          print_time_seconds: null,
          filament_used_grams: null,
          recommended: false,
        },
      ],
    },
    { part_id: 3, has_sliced_source: false, yield_min: null, yield_max: null, hidden_sources: 0, sources: [] },
  ],
};

const combinations: ProductCombinationsPage = {
  total: 2,
  page: 1,
  items: [
    {
      choices: [{ group_id: 1, group: 'Lid type', option_id: 10, option: 'Glass' }],
      available: false,
      reasons: [{ code: 'option_without_part', group: 'Lid type', option: 'Glass', part: null }],
    },
    { choices: [{ group_id: 1, group: 'Lid type', option_id: 11, option: 'Cork' }], available: true, reasons: [] },
  ],
};

function Host({ product: p = product }: { product?: Product }) {
  const heading = useRef<HTMLHeadingElement>(null);
  return (
    <>
      <h1 ref={heading} tabIndex={-1}>
        Flask
      </h1>
      <CompositionTab product={p} headingRef={heading} />
    </>
  );
}

const row = (id: number) => screen.getByTestId(`part-${id}-row`);

describe('CompositionTab', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // Granted outright: the helper's own user arrives a tick later than the first render.
    auth.granted = new Set(['orders:read', 'products:read', 'customers:read', 'stock:read', 'orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust']);
    vi.spyOn(api, 'getProductSources').mockResolvedValue(sources);
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ currency: 'EUR' } as never);
    vi.spyOn(api, 'getProductCombinations').mockResolvedValue(combinations);
  });

  describe('D01 the variants card', () => {
    it('a row per group: its options in order, the standard marked, the parts each takes', async () => {
      render(<Host />);
      const card = screen.getByTestId('composition-variants');
      expect(within(card).getByText('Lid type')).toBeInTheDocument();
      const chips = within(card).getAllByTestId(/^variant-chip-/);
      expect(chips.map((c) => c.textContent)).toEqual(['Glass · standard no parts', 'Cork Lid']);
      expect(chips[0]).toHaveAttribute('data-standard', 'true');
    });

    it('a part bound to two options lists under each of them', () => {
      const either = { ...product, parts: [...parts, part({ id: 6, name: 'Seal', variant_option_ids: [10, 11] })] };
      render(<Host product={either} />);
      const card = screen.getByTestId('composition-variants');
      expect(within(card).getAllByTestId(/^variant-chip-/).map((c) => c.textContent)).toEqual([
        'Glass · standard Seal',
        'Cork Lid, Seal',
      ]);
    });

    it('the server’s combinations table sits in the card', async () => {
      render(<Host />);
      const card = screen.getByTestId('composition-variants');
      const table = await within(card).findByTestId('product-combinations');
      expect(api.getProductCombinations).toHaveBeenCalledWith(7, 1, expect.any(Number));
      expect(await within(table).findAllByTestId('combination-row')).toHaveLength(2);
      expect(within(table).getByText('no part for «Glass» in «Lid type»')).toBeInTheDocument();
    });

    it('no groups: every order takes every part, and no combinations table', () => {
      render(<Host product={{ ...product, variant_groups: [] }} />);
      expect(screen.getByText('No variants — every order takes all parts.')).toBeInTheDocument();
      expect(screen.queryByTestId('product-combinations')).not.toBeInTheDocument();
      expect(api.getProductCombinations).not.toHaveBeenCalled();
    });
  });

  describe('D02 printed parts', () => {
    it('reads the table — no fields', async () => {
      render(<Host />);
      expect(within(row(1)).queryByRole('textbox')).not.toBeInTheDocument();
      expect(within(row(1)).getByText('Body')).toBeInTheDocument();
      expect(within(row(1)).getByText('from file')).toBeInTheDocument();
      expect(within(row(1)).getByText('× 1')).toBeInTheDocument();
      expect(within(row(1)).getByText('always')).toBeInTheDocument();
      expect(within(row(1)).getByText('body_v2')).toBeInTheDocument();
      // A zero: out of the kit (a shelf) or not counted (marked) — never an empty cell.
      expect(within(row(2)).getByText('out of kit')).toBeInTheDocument();
      expect(within(row(2)).getByText('Cork')).toHaveAttribute('title', 'Lid type: Cork');
      expect(within(row(3)).getByText('not counted')).toBeInTheDocument();
    });

    it('the variant column says the condition in short: values by a comma, groups by a dot (п. 23)', () => {
      const weight = {
        id: 2,
        name: 'Weight',
        position: 1,
        default_option_id: 21,
        lines_count: 0,
        stock_count: 0,
        parts_count: 1,
        options: [
          { id: 21, name: '400', position: 0, lines_count: 0, parts_count: 1, stock_count: 0 },
          { id: 22, name: '600', position: 1, lines_count: 0, parts_count: 1, stock_count: 0 },
        ],
      };
      const twoGroups = {
        ...product,
        variant_groups: [...product.variant_groups, weight],
        parts: [part({ id: 1, name: 'Cap', variant_option_ids: [11, 21, 22] })],
      } as unknown as Product;
      render(<Host product={twoGroups} />);
      expect(within(row(1)).getByText('Cork · 400, 600')).toHaveAttribute('title', 'Lid type: Cork · Weight: 400, 600');
    });

    it('the sources: a model chip and «pl. N · ×Y» each, in the server’s order; a hidden file says so', async () => {
      render(<Host />);
      const cells = await within(row(1)).findAllByTestId('part-source');
      expect(cells).toHaveLength(2);
      expect(cells[0]).toHaveTextContent(/^X1Cpl\. 2 · ×2$/);
      expect(cells[0]).toHaveAttribute('title', 'body.3mf');
      // Drawn the same, without a file name — and it says why.
      expect(cells[1]).toHaveTextContent(/^P1Spl\. 1 · ×1/);
      expect(cells[1]).toHaveAttribute('title', 'in a file you cannot see');
      expect(within(cells[1]).getByText('in a file you cannot see')).toBeInTheDocument();
    });

    it('sources that read alike — same model, plate and yield from several files — are one chip with the count and the files named', async () => {
      // Measured on the stand (WS-13 E9 runner): 39 sliced files gave a part 27 identical chips.
      const same = (plate_id: number, filename: string) => ({
        ...sources.parts[0].sources[0],
        plate_id,
        library_file_id: plate_id,
        filename,
        recommended: plate_id === 21,
      });
      vi.spyOn(api, 'getProductSources').mockResolvedValue({
        parts: [{ ...sources.parts[0], sources: [same(21, 'a.3mf'), same(22, 'b.3mf'), same(23, 'c.3mf'), sources.parts[0].sources[1]] }],
      });
      render(<Host />);
      const cells = await within(row(1)).findAllByTestId('part-source');
      expect(cells).toHaveLength(2);
      expect(cells[0]).toHaveTextContent(/^X1Cpl\. 2 · ×2 · 3 files$/);
      expect(cells[0]).toHaveAttribute('title', 'a.3mf, b.3mf, c.3mf');
      expect(cells[1]).toHaveTextContent(/^P1Spl\. 1 · ×1/);
    });

    it('a source not sliced says so by `sliced`, not by a missing model', async () => {
      render(<Host />);
      const [source] = await within(row(2)).findAllByTestId('part-source');
      expect(source).toHaveTextContent('not sliced');
    });

    it('a part no plate gives: the amber hint', async () => {
      render(<Host />);
      expect(await within(row(3)).findByText('no plate — link or slice a file')).toBeInTheDocument();
    });

    it('sources reading: «…» in the cells; failed: a note with a retry above, the table stays', async () => {
      let fail: (e: Error) => void = () => {};
      vi.spyOn(api, 'getProductSources').mockReturnValueOnce(new Promise((_, reject) => (fail = reject)));
      render(<Host />);
      expect(within(row(1)).getByTestId('part-sources')).toHaveTextContent('…');
      await act(async () => fail(new Error('boom')));
      expect(await screen.findByText('Could not load the parts’ plates')).toBeInTheDocument();
      expect(row(1)).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
      expect(await within(row(1)).findAllByTestId('part-source')).toHaveLength(2);
    });

    it('none: says so', () => {
      render(<Host product={{ ...product, parts: parts.filter((p) => p.kind === 'purchased') }} />);
      expect(screen.getByText('No printed parts.')).toBeInTheDocument();
    });
  });

  describe('D03 purchased parts', () => {
    it('price in the farm’s currency, a link to buy in a new tab, remarks; «—» for what is missing', async () => {
      render(<Host />);
      await waitFor(() => expect(within(row(4)).getByTestId('part-price')).toHaveTextContent('€0.50'));
      const link = within(row(4)).getByRole('link', { name: /link/ });
      expect(link).toHaveAttribute('href', 'https://shop.example/m');
      expect(link).toHaveAttribute('target', '_blank');
      expect(within(row(4)).getByText('N52')).toBeInTheDocument();
      expect(within(row(5)).getByTestId('part-price')).toHaveTextContent('—');
      expect(within(row(5)).getByTestId('part-url')).toHaveTextContent('—');
    });

    it('none: says so', () => {
      render(<Host product={{ ...product, parts: parts.filter((p) => p.kind === 'printed') }} />);
      expect(screen.getByText('No purchased parts.')).toBeInTheDocument();
    });
  });

  describe('C09–C10 the doors into the editors', () => {
    it('a reader sees no door at all', () => {
      auth.granted = new Set(['orders:read', 'products:read', 'customers:read', 'stock:read']);
      render(<Host />);
      expect(screen.queryByRole('button', { name: 'Add part' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Manage variants…' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /actions/i })).not.toBeInTheDocument();
    });

    it('no inline editing any more: no «Edit composition», no fields', () => {
      render(<Host />);
      expect(screen.queryByRole('button', { name: 'Edit composition' })).not.toBeInTheDocument();
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    });

    it('«Add part» opens the part dialog; «Cancel» sends nothing and gives the focus back', async () => {
      const create = vi.spyOn(api, 'createProductPart');
      render(<Host />);
      const add = screen.getByRole('button', { name: 'Add part' });
      add.focus();
      fireEvent.click(add);
      const dialog = await screen.findByRole('dialog', { name: 'New part' });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(add).toHaveFocus();
      expect(create).not.toHaveBeenCalled();
    });

    it('a printed row: «Edit» · «Merge into…» · «Delete»; a purchased one: «Edit» · «Delete»', async () => {
      render(<Host />);
      fireEvent.click(within(row(1)).getByRole('button', { name: 'Actions' }));
      expect((await screen.findAllByRole('menuitem')).map((m) => m.textContent)).toEqual(['Edit', 'Merge into…', 'Delete']);
      fireEvent.keyDown(document, { key: 'Escape' });
      await waitFor(() => expect(screen.queryByRole('menuitem')).not.toBeInTheDocument());
      fireEvent.click(within(row(4)).getByRole('button', { name: 'Actions' }));
      expect((await screen.findAllByRole('menuitem')).map((m) => m.textContent)).toEqual(['Edit', 'Delete']);
    });

    it('one printed part has nothing to merge into', async () => {
      render(<Host product={{ ...product, parts: parts.filter((p) => p.id === 1 || p.kind === 'purchased') }} />);
      fireEvent.click(within(row(1)).getByRole('button', { name: 'Actions' }));
      expect((await screen.findAllByRole('menuitem')).map((m) => m.textContent)).toEqual(['Edit', 'Delete']);
    });

    it('«Edit» opens the part dialog on that part', async () => {
      render(<Host />);
      fireEvent.click(within(row(4)).getByRole('button', { name: 'Actions' }));
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }));
      const dialog = await screen.findByRole('dialog', { name: 'Edit part' });
      expect(within(dialog).getByLabelText('Name')).toHaveValue('Magnet');
    });

    it('«Merge into…» opens the merge on that part; the merged row leaves and the heading takes the focus', async () => {
      const merge = vi.spyOn(api, 'mergeProductPart').mockResolvedValue(parts[0] as never);
      const { rerender } = render(<Host />);
      fireEvent.click(within(row(2)).getByRole('button', { name: 'Actions' }));
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Merge into…' }));
      const dialog = await screen.findByRole('dialog', { name: 'Merge part “Lid” into…' });
      fireEvent.change(within(dialog).getByLabelText('Target part'), { target: { value: '1' } });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Merge' }));
      await waitFor(() => expect(merge).toHaveBeenCalledWith(7, 1, 2));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      rerender(<Host product={{ ...product, parts: parts.filter((p) => p.id !== 2) }} />);
      await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: 'Flask' })).toHaveFocus());
    });

    it('«Delete» of a printed part with stock: its history goes, and the number on the shelf is said', async () => {
      render(<Host product={{ ...product, parts: parts.map((p) => (p.id === 1 ? { ...p, stock_balance: 3 } : p)) }} />);
      fireEvent.click(within(row(1)).getByRole('button', { name: 'Actions' }));
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
      const dialog = await screen.findByRole('dialog', { name: 'Delete the part?' });
      expect(dialog).toHaveTextContent(
        '“Body” leaves the product’s composition with the history of its movements on the shelf; saved order and stock lines will no longer contain this part. The next file sync creates it again if the object is on a plate.',
      );
      expect(dialog).toHaveTextContent('On the shelf now: 3 pcs — they go with the history; no write-off movement is recorded.');
    });

    it('«Delete» of a printed part with nothing on the shelf still says its history goes', async () => {
      render(<Host />);
      fireEvent.click(within(row(2)).getByRole('button', { name: 'Actions' }));
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
      const dialog = await screen.findByRole('dialog', { name: 'Delete the part?' });
      expect(dialog).toHaveTextContent('with the history of its movements on the shelf');
      expect(dialog).not.toHaveTextContent('On the shelf now');
    });

    it('«Delete» of a purchased part says its purchases go, deletes once, and gives the focus to the heading', async () => {
      const remove = vi.spyOn(api, 'deleteProductPart').mockResolvedValue(undefined as never);
      const { rerender } = render(<Host />);
      fireEvent.click(within(row(4)).getByRole('button', { name: 'Actions' }));
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
      const dialog = await screen.findByRole('dialog');
      expect(dialog).toHaveTextContent(
        '“Magnet” leaves the product’s composition with the history of its purchases in orders; saved order and stock lines will no longer contain this part.',
      );
      act(() => {
        within(dialog).getByRole('button', { name: 'Delete' }).click();
        within(dialog).getByRole('button', { name: 'Delete' }).click();
      });
      await waitFor(() => expect(remove).toHaveBeenCalledTimes(1));
      expect(remove).toHaveBeenCalledWith(7, 4);
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(await screen.findByText('Part deleted')).toBeInTheDocument();
      // The focus went back to the row's menu; the re-read takes the row away.
      expect(within(row(4)).getByRole('button', { name: 'Actions' })).toHaveFocus();
      rerender(<Host product={{ ...product, parts: parts.filter((p) => p.id !== 4) }} />);
      await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: 'Flask' })).toHaveFocus());
    });

    it('a row the re-read takes while the confirmation is still open: the heading still gets the focus', async () => {
      let rerender: (ui: ReactElement) => void = () => {};
      vi.spyOn(api, 'deleteProductPart').mockImplementation(async () => {
        rerender(<Host product={{ ...product, parts: parts.filter((p) => p.id !== 4) }} />);
        return undefined as never;
      });
      ({ rerender } = render(<Host />));
      fireEvent.click(within(row(4)).getByRole('button', { name: 'Actions' }));
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
      const dialog = await screen.findByRole('dialog');
      act(() => within(dialog).getByRole('button', { name: 'Delete' }).click());
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: 'Flask' })).toHaveFocus());
    });

    it('«Manage variants…» opens the manager; «Cancel» sends nothing and gives the focus back', async () => {
      const apply = vi.spyOn(api, 'applyProductVariants');
      render(<Host />);
      const manage = screen.getByRole('button', { name: 'Manage variants…' });
      manage.focus();
      fireEvent.click(manage);
      const dialog = await screen.findByRole('dialog', { name: 'Product variants' });
      fireEvent.change(within(dialog).getAllByLabelText('Group')[0], { target: { value: 'Cap type' } });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(manage).toHaveFocus();
      expect(apply).not.toHaveBeenCalled();
    });
  });
});

describe('the parts’ pictures (spec part-thumbnails §12.2)', () => {
  it('a printed row shows its picture beside the name; a purchased row its glyph; a queued one says so', async () => {
    vi.spyOn(api, 'getProductSources').mockResolvedValue({ parts: [] } as never);
    const withPictures = {
      ...product,
      parts: [
        part({ id: 1, name: 'Body', image: { kind: 'render', status: 'ready', v: 'r1' } }),
        part({ id: 2, name: 'Lid', image: { kind: 'render', status: 'pending', v: null } }),
        part({ id: 4, name: 'Magnet', kind: 'purchased', image: null }),
      ],
    } as Product;
    render(<Host product={withPictures} />);
    const body = screen.getByTestId('part-1-row');
    expect(within(body).getByRole('button', { name: 'Show Body larger' })).toBeInTheDocument();
    expect(within(body).getByTestId('part-thumb-img')).toHaveAttribute('src', '/api/v1/product-parts/1/image?v=r1&size=sm');
    expect(within(screen.getByTestId('part-2-row')).getByRole('img', { name: 'Lid: the picture is being prepared' })).toBeInTheDocument();
    expect(within(screen.getByTestId('part-4-row')).getByTestId('part-thumb').querySelector('[data-glyph="purchased"]')).not.toBeNull();
  });
});
