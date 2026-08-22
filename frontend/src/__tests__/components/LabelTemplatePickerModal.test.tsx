/**
 * Choosing what to print, from what actually exists.
 *
 * This dialog used to offer six buttons hard-coded in this component, each with
 * a translated title and hint, while the catalogue they were meant to represent
 * sat in the database being ignored — adding a design added nothing here, and
 * renaming one renamed nothing. It then briefly offered the catalogue as a grid
 * of cards that printed on click, which does not survive somebody drawing a
 * dozen designs: the list grows downward forever, and the same click that makes
 * the choice acts on it.
 *
 * What it is now: pick the paper, pick the design, press Print. On the device
 * route there is no paper and no Print — the printer button is what prints.
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

const DESIGN = /^Design$/;
const PAPER = /Paper/;

/** Open a card dropdown and take one of its rows. */
const choose = async (which: RegExp, option: RegExp) => {
  fireEvent.click(await screen.findByRole('combobox', { name: which }));
  fireEvent.click(screen.getByRole('option', { name: option }));
};

const press = () => fireEvent.click(screen.getByRole('button', { name: /^Print$/ }));

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
  vi.spyOn(window, 'open').mockImplementation(() => ({ location: { href: '' } }) as Window);
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

    fireEvent.click(await screen.findByRole('combobox', { name: DESIGN }));

    const box = screen.getByRole('option', { name: /Box label 40 × 30/ });
    expect(box).toHaveTextContent('Good for filament bags');
    expect(box).toHaveTextContent('40×30');
    expect(screen.getByRole('option', { name: /Shelf tag/ })).toBeInTheDocument();
  });

  it('will not print until a design is chosen', async () => {
    // ⚠️ It opens on a placeholder rather than on whatever sorts first: Print
    // is a deliberate act and should not arrive pre-aimed.
    show();

    expect(await screen.findByRole('button', { name: /^Print$/ })).toBeDisabled();

    await choose(DESIGN, /Box label 40 × 30/);

    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled();
  });

  it('says so when nothing is drawn yet rather than showing an empty strip', async () => {
    vi.mocked(api.getLabelTemplates).mockResolvedValue([] as never);
    show();

    expect(await screen.findByText(/No design is drawn for this/i)).toBeInTheDocument();
  });

  it('prints the design by id (#1870 monochrome defaults off)', async () => {
    show();

    await choose(DESIGN, /Box label 40 × 30/);
    press();

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
    await choose(DESIGN, /Box label 40 × 30/);
    press();

    await waitFor(() => {
      expect(api.printSpoolLabels).toHaveBeenCalledWith(
        expect.objectContaining({ template_id: 7, monochrome: true }),
      );
    });
  });

  it('threads monochrome through the Spoolman endpoint too (#1870)', async () => {
    show({ spoolmanMode: true });

    fireEvent.click(screen.getByText(/black & white printer/i));
    await choose(DESIGN, /Box label 40 × 30/);
    press();

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

    await choose(DESIGN, /Box label 40 × 30/);
    press();

    await waitFor(() => expect(api.printSpoolLabels).toHaveBeenCalled());
    expect(vi.mocked(api.printSpoolLabels).mock.calls[0][0]).not.toHaveProperty('sheet_id');
  });

  it('lays the design out on the paper you pick', async () => {
    vi.mocked(api.getLabelSheets).mockResolvedValue([L7160] as never);
    show();

    await choose(PAPER, /Avery L7160/);
    await choose(DESIGN, /Box label 40 × 30/);
    press();

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

    await choose(PAPER, /Avery 5160/);
    fireEvent.click(screen.getByRole('combobox', { name: DESIGN }));

    const option = screen.getByRole('option', { name: /Box label 40 × 30/ });
    expect(option).toBeDisabled();
    expect(option).toHaveTextContent(/prints at its own size or not at all/i);
  });
});

describe('what happens to the finished PDF', () => {
  const printOne = async () => {
    await choose(DESIGN, /Box label 40 × 30/);
    press();
  };

  it('opens a tab on the click, before the render is even asked for', async () => {
    // ⚠️ This is the whole point. Rendering a sheet takes a round trip, and a
    // window.open after the await is a popup — blocked by default — which sent
    // the code down its download fallback. A sheet of labels should arrive in a
    // tab where the person decides to print it or save it.
    let resolve: (blob: Blob) => void = () => {};
    vi.mocked(api.printSpoolLabels).mockReturnValue(
      new Promise<Blob>((done) => {
        resolve = done;
      }),
    );
    const tab = { location: { href: '' }, close: vi.fn() };
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    show();

    await printOne();

    expect(open).toHaveBeenCalledTimes(1);
    expect(tab.location.href).toBe(''); // claimed, with nothing to show yet

    resolve(PDF_BLOB);
    await waitFor(() => expect(tab.location.href).toBe('blob:mock'));
  });

  it('closes the blank tab when the render fails', async () => {
    // Otherwise the empty tab sits in front of the error message explaining it.
    vi.mocked(api.printSpoolLabels).mockRejectedValue(new Error('nope'));
    const tab = { location: { href: '' }, close: vi.fn() };
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    show();

    await printOne();

    await waitFor(() => expect(tab.close).toHaveBeenCalled());
  });

  it('falls back to a download only when the popup is blocked outright', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    const click = vi.fn();
    const anchor = document.createElement('a');
    anchor.click = click;
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) =>
      tag === 'a' ? anchor : document.createElementNS('http://www.w3.org/1999/xhtml', tag)) as never);
    show();

    await printOne();

    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(anchor.download).toBe('bamdude-labels.pdf');
  });
});

describe('when a label printer is set up', () => {
  beforeEach(() => {
    vi.mocked(api.getSettings).mockResolvedValue({ device_labels_enabled: true } as never);
    vi.mocked(api.getLabelDevices).mockResolvedValue([DEVICE] as never);
    vi.mocked(api.createLabelJobs).mockResolvedValue([{ id: 1 }] as never);
    vi.mocked(api.getLabelTemplates).mockResolvedValue([
      design(),
      design({ id: 9, name: 'Roll 50 × 30', target: 'thermal', builtin_key: null, is_builtin: false }),
    ] as never);
  });

  it('asks which way the batch goes before offering designs', async () => {
    show();

    expect(await screen.findByText(/How should these print/i)).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: DESIGN })).not.toBeInTheDocument();
  });

  it('offers only driver designs down the driver route', async () => {
    show();

    fireEvent.click(await screen.findByText(/Through a printer on this computer/i));
    fireEvent.click(screen.getByRole('combobox', { name: DESIGN }));

    expect(screen.getByRole('option', { name: /Box label 40 × 30/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Roll 50 × 30/ })).not.toBeInTheDocument();
  });

  it('offers only thermal designs down the device route', async () => {
    // ⚠️ The split exists because colour cannot survive a one-bit head. A
    // driver design may be built around a filled swatch; offering it here would
    // be offering a label that arrives missing its subject.
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));
    fireEvent.click(screen.getByRole('combobox', { name: DESIGN }));

    expect(screen.getByRole('option', { name: /Roll 50 × 30/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Box label 40 × 30/ })).not.toBeInTheDocument();
  });

  it('never offers a way to make a PDF on the device route', async () => {
    // ⚠️ A PDF is a download nobody wants for a printer standing on the desk,
    // so there is no Print button here at all — the printer is the button.
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));

    expect(screen.queryByRole('button', { name: /^Print$/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Niimbot B1'));
    await waitFor(() => expect(api.createLabelJobs).toHaveBeenCalled());
    expect(api.printSpoolLabels).not.toHaveBeenCalled();
  });

  it('sends the design you chose', async () => {
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));
    await choose(DESIGN, /Roll 50 × 30/);
    fireEvent.click(screen.getByText('Niimbot B1'));

    // ⚠️ Read off mock.calls rather than toHaveBeenCalledWith: TanStack hands
    // the mutation function a second argument, so a whole-call match never
    // holds however right the body is.
    await waitFor(() => expect(api.createLabelJobs).toHaveBeenCalled());
    expect(vi.mocked(api.createLabelJobs).mock.calls[0][0]).toMatchObject({
      device_id: 5,
      template_id: 9,
    });
  });

  it('sends no template_id when the design is left to the printer', async () => {
    // ⚠️ Absent, not null. The server then picks the design whose size matches
    // the loaded stock, and refuses rather than guessing when nothing does.
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));
    fireEvent.click(screen.getByText('Niimbot B1'));

    await waitFor(() => expect(api.createLabelJobs).toHaveBeenCalled());
    expect(vi.mocked(api.createLabelJobs).mock.calls[0][0]).not.toHaveProperty('template_id');
  });

  it('offers no paper on the device route', async () => {
    // A desk label printer feeds a roll. There is no page to tile.
    vi.mocked(api.getLabelSheets).mockResolvedValue([AVERY] as never);
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));

    expect(screen.queryByRole('combobox', { name: PAPER })).not.toBeInTheDocument();
  });

  it('refuses a printer whose stock is smaller than the design, and says why', async () => {
    // ⚠️ Judged per printer, not per design: two desk printers can have
    // different rolls loaded, so "does not fit" is a fact about the pair.
    vi.mocked(api.getLabelTemplates).mockResolvedValue([
      design({ id: 9, name: 'Big roll', target: 'thermal', width_mm: 60, height_mm: 40 }),
    ] as never);
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));
    await choose(DESIGN, /Big roll/);

    expect(screen.getByText(/prints at its own size or not at all/i)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Niimbot B1'));
    expect(api.createLabelJobs).not.toHaveBeenCalled();
  });

  it('allows a design smaller than the loaded stock', async () => {
    // ⚠️ The server refuses only what is LARGER. A smaller label prints with a
    // margin, and greying it out would forbid something that works.
    vi.mocked(api.getLabelTemplates).mockResolvedValue([
      design({ id: 9, name: 'Tiny', target: 'thermal', width_mm: 20, height_mm: 10 }),
    ] as never);
    show();

    fireEvent.click(await screen.findByText(/On a label printer/i));
    await choose(DESIGN, /Tiny/);
    fireEvent.click(screen.getByText('Niimbot B1'));

    await waitFor(() => expect(api.createLabelJobs).toHaveBeenCalled());
  });

  it('skips the question entirely when no device is adopted', async () => {
    vi.mocked(api.getLabelDevices).mockResolvedValue([] as never);
    show();

    expect(await screen.findByRole('combobox', { name: DESIGN })).toBeInTheDocument();
    expect(screen.queryByText(/How should these print/i)).not.toBeInTheDocument();
  });
});
