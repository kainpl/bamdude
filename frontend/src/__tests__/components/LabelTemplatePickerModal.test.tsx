/**
 * Choosing what to print, from what actually exists.
 *
 * This dialog used to offer six buttons hard-coded in this component, each with
 * a translated title and hint, while the catalogue they were meant to represent
 * sat in the database being ignored — adding a design added nothing here, and
 * renaming one renamed nothing. These cover the shape that replaced them: the
 * list is the catalogue, filtered by where the batch is going.
 *
 * BamDude posts ``{ spools: [{ id, display_name }], template_id, monochrome }``
 * — the ``spools`` object shape carries the per-spool display-name override,
 * which diverges from upstream's flat ``spool_ids`` payload.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { LabelTemplatePickerModal } from '../../components/LabelTemplatePickerModal';
import type { InventorySpool } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    printSpoolLabels: vi.fn(),
    printSpoolmanSpoolLabels: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
    getLabelTemplates: vi.fn(),
    getLabelSheets: vi.fn(),
    getLabelDevices: vi.fn(),
    createLabelJobs: vi.fn(),
  },
}));

import { api } from '../../api/client';

const PDF_BLOB = new Blob([new Uint8Array([0x25, 0x50, 0x44, 0x46])], { type: 'application/pdf' });

const SPOOLS = [
  { id: 1, material: 'PLA', subtype: 'Basic', brand: 'Polymaker', color_name: 'Red', rgba: 'FF0000FF' },
  { id: 2, material: 'PETG', subtype: null, brand: 'Sunlu', color_name: 'Blue', rgba: '0000FFFF' },
] as unknown as InventorySpool[];

const design = (over: Record<string, unknown> = {}) => ({
  id: 7,
  name: 'Box label 40 × 30',
  description: 'Good for filament bags',
  width_mm: 40,
  height_mm: 30,
  shape: 'rect',
  target: 'driver',
  elements: [],
  builtin_key: 'box_40x30',
  is_builtin: true,
  ...over,
});

const AVERY = {
  id: 3,
  name: 'Avery 5160',
  builtin_key: 'avery_5160',
  page_size: 'letter',
  cell_width_mm: 66.675,
  cell_height_mm: 25.4,
  cols: 3,
  rows: 10,
  margin_top_mm: 12.7,
  margin_left_mm: 4.76,
  gap_x_mm: 3.175,
  gap_y_mm: 0,
  is_builtin: true,
  overflow: [],
};

/** A4 stock with roomier cells — 63.5 × 38.1mm holds the 40 × 30 design. */
const L7160 = {
  ...AVERY,
  id: 4,
  name: 'Avery L7160',
  builtin_key: 'avery_l7160',
  page_size: 'A4',
  cell_width_mm: 63.5,
  cell_height_mm: 38.1,
  rows: 7,
};

const DEVICE = {
  id: 5,
  name: 'Niimbot B1',
  model: 'B1',
  installation_id: 'abc',
  enabled: true,
  printer_reachable: true,
  cassette_width_mm: 50,
  cassette_height_mm: 30,
};

const show = (over: Record<string, unknown> = {}) =>
  render(
    <LabelTemplatePickerModal
      isOpen={true}
      onClose={vi.fn()}
      availableSpools={SPOOLS}
      initialSelectedIds={[1]}
      spoolmanMode={false}
      {...over}
    />,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.printSpoolLabels).mockResolvedValue(PDF_BLOB);
  vi.mocked(api.printSpoolmanSpoolLabels).mockResolvedValue(PDF_BLOB);
  vi.mocked(api.getLabelTemplates).mockResolvedValue([design()] as never);
  vi.mocked(api.getLabelSheets).mockResolvedValue([] as never);
  vi.mocked(api.getLabelDevices).mockResolvedValue([] as never);
  vi.mocked(api.getSettings).mockResolvedValue({} as never);
  Object.defineProperty(window.URL, 'createObjectURL', {
    value: vi.fn(() => 'blob:mock'),
    configurable: true,
  });
  Object.defineProperty(window.URL, 'revokeObjectURL', {
    value: vi.fn(),
    configurable: true,
  });
  vi.spyOn(window, 'open').mockImplementation(() => ({}) as Window);
});

describe('LabelTemplatePickerModal', () => {
  it('renders the monochrome toggle when open', () => {
    show();

    expect(screen.getByText(/Print spool labels/i)).toBeInTheDocument();
    expect(screen.getByText(/black & white printer/i)).toBeInTheDocument();
  });

  it('offers the designs that exist, with what each is for', async () => {
    vi.mocked(api.getLabelTemplates).mockResolvedValue([
      design(),
      design({ id: 8, name: 'Shelf tag', description: 'Mine', builtin_key: null, is_builtin: false }),
    ] as never);
    show();

    expect(await screen.findByText('Box label 40 × 30')).toBeInTheDocument();
    expect(screen.getByText('Good for filament bags')).toBeInTheDocument();
    expect(screen.getByText('Shelf tag')).toBeInTheDocument();
  });

  it('says so when nothing is drawn yet rather than showing an empty strip', async () => {
    vi.mocked(api.getLabelTemplates).mockResolvedValue([] as never);
    show();

    expect(await screen.findByText(/No design is drawn for this/i)).toBeInTheDocument();
  });

  it('prints the design by id (#1870 monochrome defaults off)', async () => {
    show();

    fireEvent.click(await screen.findByText('Box label 40 × 30'));

    await waitFor(() => {
      expect(api.printSpoolLabels).toHaveBeenCalledWith(
        expect.objectContaining({
          template_id: 7,
          monochrome: false,
          spools: expect.arrayContaining([expect.objectContaining({ id: 1 })]),
        }),
      );
    });
  });

  it('sends monochrome:true when the black & white checkbox is ticked (#1870)', async () => {
    show();

    fireEvent.click(screen.getByText(/black & white printer/i));
    fireEvent.click(await screen.findByText('Box label 40 × 30'));

    await waitFor(() => {
      expect(api.printSpoolLabels).toHaveBeenCalledWith(
        expect.objectContaining({ template_id: 7, monochrome: true }),
      );
    });
  });

  it('threads monochrome through the Spoolman endpoint too (#1870)', async () => {
    show({ spoolmanMode: true });

    fireEvent.click(screen.getByText(/black & white printer/i));
    fireEvent.click(await screen.findByText('Box label 40 × 30'));

    await waitFor(() => {
      expect(api.printSpoolmanSpoolLabels).toHaveBeenCalledWith(
        expect.objectContaining({ template_id: 7, monochrome: true }),
      );
    });
  });

  it('sends no sheet_id when printing one per page', async () => {
    // ⚠️ Absent, not null: the field means "lay this out on that paper", and
    // the backend reads its presence.
    vi.mocked(api.getLabelSheets).mockResolvedValue([L7160] as never);
    show();

    fireEvent.click(await screen.findByText('Box label 40 × 30'));

    await waitFor(() => expect(api.printSpoolLabels).toHaveBeenCalled());
    expect(vi.mocked(api.printSpoolLabels).mock.calls[0][0]).not.toHaveProperty('sheet_id');
  });

  it('lays the design out on the paper you pick', async () => {
    vi.mocked(api.getLabelSheets).mockResolvedValue([L7160] as never);
    show();

    fireEvent.change(await screen.findByLabelText(/Paper/i), { target: { value: '4' } });
    fireEvent.click(screen.getByText('Box label 40 × 30'));

    await waitFor(() => {
      expect(api.printSpoolLabels).toHaveBeenCalledWith(
        expect.objectContaining({ template_id: 7, sheet_id: 4 }),
      );
    });
  });

  it('refuses a design taller than the cells, and says why', async () => {
    // ⚠️ Said here as well as refused there. A label prints at its own size or
    // not at all — Avery 5160's cells are 25.4mm tall and this design is 30.
    vi.mocked(api.getLabelSheets).mockResolvedValue([AVERY] as never);
    show();

    fireEvent.change(await screen.findByLabelText(/Paper/i), { target: { value: '3' } });

    expect(screen.getByText(/prints at its own size or not at all/i)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Box label 40 × 30'));
    expect(api.printSpoolLabels).not.toHaveBeenCalled();
  });
});

describe('when a label printer is set up', () => {
  beforeEach(() => {
    vi.mocked(api.getSettings).mockResolvedValue({ device_labels_enabled: true } as never);
    vi.mocked(api.getLabelDevices).mockResolvedValue([DEVICE] as never);
    vi.mocked(api.getLabelTemplates).mockResolvedValue([
      design(),
      design({ id: 9, name: 'Roll 50 × 30', target: 'thermal', builtin_key: null, is_builtin: false }),
    ] as never);
  });

  it('asks which way the batch goes before offering designs', async () => {
    show();

    expect(await screen.findByText(/How should these print/i)).toBeInTheDocument();
    expect(screen.queryByText('Box label 40 × 30')).not.toBeInTheDocument();
  });

  it('offers only driver designs down the driver route', async () => {
    show();

    fireEvent.click(await screen.findByText(/Through a printer on this computer/i));

    expect(screen.getByText('Box label 40 × 30')).toBeInTheDocument();
    expect(screen.queryByText('Roll 50 × 30')).not.toBeInTheDocument();
  });

  it('offers only thermal designs down the device route', async () => {
    // ⚠️ The split exists because colour cannot survive a one-bit head. A
    // driver design may be built around a filled swatch; offering it here would
    // be offering a label that arrives missing its subject.
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));

    expect(screen.getByText('Roll 50 × 30')).toBeInTheDocument();
    expect(screen.queryByText('Box label 40 × 30')).not.toBeInTheDocument();
  });

  it('offers no paper on the device route', async () => {
    // A desk label printer feeds a roll. There is no page to tile.
    vi.mocked(api.getLabelSheets).mockResolvedValue([AVERY] as never);
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));

    expect(screen.queryByLabelText(/Paper/i)).not.toBeInTheDocument();
  });

  it('skips the question entirely when no device is adopted', async () => {
    vi.mocked(api.getLabelDevices).mockResolvedValue([] as never);
    show();

    expect(await screen.findByText('Box label 40 × 30')).toBeInTheDocument();
    expect(screen.queryByText(/How should these print/i)).not.toBeInTheDocument();
  });
});
