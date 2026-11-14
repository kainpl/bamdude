import { expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import type {
  LibraryFileListItem,
  ProductListItem,
  ProductListPage,
  ProductPartRow,
  StockSuggestion,
} from '../../../../api/client';
import { PART_SOURCES_DEFAULTS, PRODUCT_ROW_DEFAULTS } from '../../../wireDefaults';

/**
 * The dialog's «Add» once it may be pressed: a picked product with variant groups waits for the
 * server's word on its combination (spec product-part-variant-combinations п. 25) — a click before
 * it lands on a disabled button and sends nothing.
 */
export async function enabledButton(name: string): Promise<HTMLElement> {
  const button = screen.getByRole('button', { name });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

const row = (over: Partial<ProductListItem>): ProductListItem => ({
  ...PRODUCT_ROW_DEFAULTS,
  id: 1,
  code: 'PR-0001',
  name: 'Pipe',
  is_active: true,
  sku: 'PP-1',
  version: null,
  category: { id: 7, name: 'Pipes' },
  status: 'ready',
  origin: 'catalog',
  origin_file_id: null,
  origin_plate_index: null,
  cover_image_filename: null,
  has_cover: false,
  parts_count: 3,
  plates_count: 1,
  lines_count: 0,
  kits_available: 3,
  finished_available: 2,
  materials: ['PETG'],
  colors: ['BLACK'],
  models: ['P1S'],
  ...over,
});

/** The Pipe: one group, Tail — straight (standard) or angled; the catalog row carries it (WS-13 E5 H01). */
export const pipe = row({
  variant_group_names: ['Tail'],
  variant_groups: [
    { id: 10, name: 'Tail', default_option_id: 100, options: [{ id: 100, name: 'straight' }, { id: 101, name: 'angled' }] },
  ],
});
export const lamp = row({
  id: 2,
  code: 'PR-0002',
  name: 'Lamp',
  sku: null,
  status: 'draft',
  kits_available: 1,
  finished_available: 0,
  materials: ['PLA'],
  colors: [],
  models: ['X1C'],
});
export const vase = row({ id: 3, code: 'PR-0003', name: 'Vase', sku: null, kits_available: 0, finished_available: 0 });

export const pageOf = (items: ProductListItem[], total: number, page = 1): ProductListPage => ({
  items,
  meta: { total, current_page: page, per_page: 24, last_page: Math.max(1, Math.ceil(total / 24)) },
  categories: [],
  catalog_total: total,
  uncategorized: 0,
  all_categories: total,
});

/** The Pipe's detail: one group, Tail, straight (standard) or angled. */
export const pipeDetail = {
  ...pipe,
  description: null,
  notes: null,
  designer: null,
  license: null,
  source_url: null,
  design_id: null,
  attachments: [],
  parts: [],
  library_file_ids: [],
  library_folder_ids: [],
  units_printed_total: 0,
  variant_groups: [
    {
      id: 10,
      name: 'Tail',
      position: 0,
      default_option_id: 100,
      options: [
        { id: 100, name: 'straight', position: 0, lines_count: 0, parts_count: 1, stock_count: 0 },
        { id: 101, name: 'angled', position: 1, lines_count: 0, parts_count: 1, stock_count: 0 },
      ],
    },
  ],
};

export const suggestion = (over: Partial<StockSuggestion>): StockSuggestion => ({
  product_id: 1,
  finished_free: 2,
  kits_free: 3,
  from_finished: 2,
  from_kits: 3,
  to_print: 1,
  position_id: 5,
  position_code: 'SK-0005',
  ...over,
});


export const part = (over: Partial<ProductPartRow>): ProductPartRow => ({
  ...PART_SOURCES_DEFAULTS,
  part_id: 11,
  name: 'Tail',
  variants: [],
  product: { id: 1, code: 'PR-0001', name: 'Pipe', sku: 'PP-1' },
  models: ['P1S'],
  ...over,
});

export const partsPage = (items: ProductPartRow[], total = items.length) => ({
  items,
  meta: { total, current_page: 1, per_page: 24, last_page: Math.max(1, Math.ceil(total / 24)) },
});

export const libraryFile = (id: number, filename: string, over: Record<string, unknown> = {}) =>
  ({
    id,
    filename,
    folder_id: null,
    file_type: 'gcode',
    // A sliced .gcode.3mf: the server's tags say so, and its type can be planned (WS-13 E5 H02).
    file_tags: ['gcode', '3mf'],
    plan_eligible: true,
    sliced_for_model: null,
    print_name: null,
    ...over,
  }) as unknown as LibraryFileListItem;

export const filesPage = (items: LibraryFileListItem[]) => ({
  items,
  meta: { total: items.length, current_page: 1, per_page: 24, last_page: 1 },
});

export const plate = (index: number, over: Record<string, unknown> = {}) => ({
  index,
  name: null,
  objects: ['a', 'b', 'c'],
  object_count: 3,
  has_thumbnail: false,
  thumbnail_url: null,
  print_time_seconds: 5400,
  filament_used_grams: 12,
  filaments: [],
  ...over,
});
