/**
 * WS-13 E5 D — «Parts of a product»: each part with the file and plate the plan
 * would print it from (the server's `recommended` source, E1 PS1), the yield on that
 * plate, and a plate-count preview over the server's numbers (PS8) — never a plate
 * picked by the dialog, never a read of the library.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
import { render } from '../../../utils';
import { api, ApiError } from '../../../../api/client';
import type { PartSource } from '../../../../api/client';
import { AddToOrderDialog } from '../../../../components/projects/add-to-order/AddToOrderDialog';
import { pageOf, part, partsPage, pipe } from './fixtures';

const ORDER = { id: 5, code: 'OR-0005', name: 'Flasks for Acme', active: true };

const source = (over: Partial<PartSource>): PartSource => ({
  plate_id: 1,
  library_file_id: 31,
  filename: 'clm01_x1c.gcode.3mf',
  folder_id: null,
  folder_name: null,
  hidden: false,
  plate_index: 2,
  printer_model: 'X1C',
  sliced: true,
  yield: 12,
  print_time_seconds: 3600,
  filament_used_grams: 40,
  recommended: false,
  ...over,
});

const sliced = part({
  part_id: 11,
  name: 'Lid',
  variants: [{ group: 'Mount', options: ['DIN'] }],
  models: ['X1C', 'P1S'],
  sources: [source({ recommended: true }), source({ plate_id: 2, filename: 'clm01_p1s.gcode.3mf', printer_model: 'P1S', yield: 6 })],
  has_sliced_source: true,
  yield_min: 6,
  yield_max: 12,
});
const same = part({
  part_id: 12,
  name: 'Clip',
  sources: [source({ plate_id: 3, plate_index: 0, recommended: true })],
  has_sliced_source: true,
  yield_min: 12,
  yield_max: 12,
});
const hiddenFile = part({
  part_id: 13,
  name: 'Base',
  sources: [source({ plate_id: 4, filename: null, hidden: true, recommended: true })],
  has_sliced_source: true,
  yield_min: 12,
  yield_max: 12,
});
const unsliced = part({ part_id: 14, name: 'Button', models: [] });

describe('the parts tab of «Add to order» (WS-13 E5 D)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProductsPaged').mockResolvedValue(pageOf([pipe], 1));
    vi.spyOn(api, 'getProductCategories').mockResolvedValue([]);
    vi.spyOn(api, 'getProductFacets').mockResolvedValue({ materials: [], colors: [], models: ['P1S', 'X1C'] });
    vi.spyOn(api, 'suggestStock').mockResolvedValue({ items: [] });
    vi.spyOn(api, 'getProductParts').mockResolvedValue(partsPage([sliced, same, hiddenFile, unsliced]));
    vi.spyOn(api, 'getLibraryFilesPaged');
    vi.spyOn(api, 'getLibraryFolders');
  });

  async function openParts() {
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    await screen.findByTestId('add-product-1');
    fireEvent.click(screen.getByRole('tab', { name: 'Parts of a product' }));
    return within(await screen.findByTestId('add-part-11'));
  }

  it('heads its five columns in the mockup’s order', async () => {
    await openParts();
    const heads = [...within(screen.getByRole('tabpanel')).getAllByRole('columnheader')].map((h) => h.textContent);
    expect(heads).toEqual(['Pick', 'Part / product', 'File · printer', 'On the plate', 'Order, pcs']);
  });

  it('names the part with its option, and the product under it', async () => {
    const row = await openParts();
    expect(row.getByText('DIN')).toHaveAttribute('title', 'Mount: DIN');
    expect(row.getByText('PP-1')).toHaveClass('font-mono');
    expect(row.getByText(/Pipe/)).toBeInTheDocument();
  });

  it('shows the recommended file and plate, the models, and the yield on that plate', async () => {
    const row = await openParts();
    expect(row.getByText('clm01_x1c.gcode.3mf · plate 2')).toBeInTheDocument();
    expect(row.getByText('X1C')).toBeInTheDocument();
    expect(row.getByText('P1S')).toBeInTheDocument();
    expect(row.getByText('12 pcs')).toBeInTheDocument();
    expect(within(screen.getByTestId('add-part-12')).getByText('clm01_x1c.gcode.3mf · whole file')).toBeInTheDocument();
  });

  it('keeps a file the reader may not open nameless, its numbers kept', async () => {
    await openParts();
    const row = within(screen.getByTestId('add-part-13'));
    expect(row.getByText('File you cannot open · plate 2')).toBeInTheDocument();
    expect(row.getByText('12 pcs')).toBeInTheDocument();
  });

  it('says when nothing is sliced for a part', async () => {
    await openParts();
    const row = within(screen.getByTestId('add-part-14'));
    expect(row.getByText('no sliced plate')).toBeInTheDocument();
    expect(row.getByText('—')).toBeInTheDocument();
  });

  it('previews the plates over the server’s yields — one number, a range, or unknown (PS8)', async () => {
    const row = await openParts();
    fireEvent.click(row.getByRole('checkbox'));
    fireEvent.change(row.getByLabelText('Order, pcs'), { target: { value: '30' } });
    expect(row.getByText('≈ 3–5 plates')).toBeInTheDocument();

    const clip = within(screen.getByTestId('add-part-12'));
    fireEvent.click(clip.getByRole('checkbox'));
    fireEvent.change(clip.getByLabelText('Order, pcs'), { target: { value: '30' } });
    expect(clip.getByText('≈ 3 plates')).toBeInTheDocument();

    const button = within(screen.getByTestId('add-part-14'));
    fireEvent.click(button.getByRole('checkbox'));
    expect(button.getByText('plates: unknown')).toBeInTheDocument();
  });

  it('reads nothing from the library', async () => {
    await openParts();
    expect(api.getLibraryFilesPaged).not.toHaveBeenCalled();
    expect(api.getLibraryFolders).not.toHaveBeenCalled();
  });

  it('offers the products without sliced files as a filter', async () => {
    await openParts();
    const filter = within(screen.getByRole('tabpanel')).getByLabelText('Printer model');
    expect(within(filter).getByRole('option', { name: 'No sliced files' })).toHaveValue('none');
  });

  it('says a failed list with retry, and an empty search', async () => {
    const get = vi.spyOn(api, 'getProductParts').mockRejectedValue(new ApiError('boom', 500));
    render(<AddToOrderDialog order={ORDER} onClose={() => {}} />);
    await screen.findByTestId('add-product-1');
    fireEvent.click(screen.getByRole('tab', { name: 'Parts of a product' }));
    expect(await screen.findByText('Could not load the parts')).toBeInTheDocument();
    get.mockResolvedValue(partsPage([]));
    fireEvent.click(within(screen.getByRole('tabpanel')).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Change the search or a filter.')).toBeInTheDocument();
  });
});
