/**
 * The Slice dialog's two farm filters, rendered against a served listing and a
 * served farm state: "only printers that are online" and "only filaments that
 * are loaded". The matching rules are unit-tested in `utils/sliceFarmFilter`;
 * these pin what the dialog does with them — off by default, remembered per
 * browser, never replacing the chosen preset, and saying why a list is empty.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { SliceModal } from '../../components/SliceModal';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import type { LoadedFilamentsResponse, UnifiedPreset, UnifiedPresetsBySlot } from '../../api/client';

type Tier = 'local' | 'orca_cloud' | 'cloud' | 'standard';
type Listing = Partial<Record<Tier, Partial<Record<keyof UnifiedPresetsBySlot, Partial<UnifiedPreset>[]>>>>;

const H2D = 'Bambu Lab H2D 0.4 nozzle';
const A1 = 'Bambu Lab A1 0.4 nozzle';
const X1C = 'Bambu Lab X1 Carbon 0.4 nozzle';
const ONLINE_KEY = 'bamdude:slice-modal:only-online-printers';
const LOADED_KEY = 'bamdude:slice-modal:only-loaded-filaments';

const LISTING: Listing = {
  local: { printer: [{ name: 'Garage machine', inherits: H2D }] },
  standard: {
    printer: [{ name: A1 }, { name: H2D }, { name: X1C }],
    process: [{ name: '0.20mm Standard @BBL A1' }, { name: '0.20mm Standard @BBL H2D' }],
    filament: [
      { name: 'Bambu PLA Basic @BBL H2D', filament_type: 'PLA' },
      { name: 'Generic PLA @BBL H2D', filament_type: 'PLA' },
      { name: 'Generic PETG @BBL H2D', filament_type: 'PETG' },
    ],
  },
};

const FARM: LoadedFilamentsResponse = {
  printers: [
    {
      printer_id: 1,
      model: 'H2D',
      accepts: [],
      nozzle_diameters: [0.4],
      filaments: [{ kind: 'ams', material: 'PLA', family_id: 'GFA00', by_material: false }],
    },
  ],
  families: {
    GFA00: {
      name: 'Bambu PLA Basic',
      cloud: ['GFSA00'],
      orca_cloud: [],
      local: [],
      standard: ['Bambu PLA Basic @BBL H2D'],
    },
  },
};

let farmRequests = 0;

function serve(farm: LoadedFilamentsResponse | 'error' = FARM, listing: Listing = LISTING) {
  const tier = (source: Tier) => {
    const of = (slot: keyof UnifiedPresetsBySlot) =>
      (listing[source]?.[slot] ?? []).map((p) => ({ id: p.name, source, ...p }));
    return { printer: of('printer'), process: of('process'), filament: of('filament') };
  };
  farmRequests = 0;
  server.use(
    http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] })),
    http.get('*/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({
        file_id: 1,
        filename: 'Cube.3mf',
        filaments: [{ slot_id: 1, type: 'PLA', color: '#FFFFFF', used_grams: 1, used_meters: 1 }],
      }),
    ),
    http.get('*/api/v1/slicer/presets', () =>
      HttpResponse.json({
        local: tier('local'),
        orca_cloud: tier('orca_cloud'),
        cloud: tier('cloud'),
        standard: tier('standard'),
        cloud_status: 'ok',
        orca_cloud_status: 'ok',
      }),
    ),
    http.get('*/api/v1/slicer/printer-models', () =>
      HttpResponse.json({ 'Bambu Lab H2D': 'H2D', 'Bambu Lab A1': 'A1', 'Bambu Lab X1 Carbon': 'X1C' }),
    ),
    http.get('*/api/v1/slicer/loaded-filaments', () => {
      farmRequests += 1;
      return farm === 'error' ? HttpResponse.json({ detail: 'boom' }, { status: 500 }) : HttpResponse.json(farm);
    }),
    http.get('*/api/v1/slicer/health/:kind', () => HttpResponse.json({ healthy: false, error: 'offline', url: null })),
    http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
    http.get('*/api/v1/slicer/pipelines', () => HttpResponse.json({ pipelines: [] })),
  );
}

function open() {
  localStorage.setItem('bamdude:presets:only-farm', 'off');
  localStorage.setItem('bamdude:presets:only-mine', 'off');
  return render(
    <SliceJobTrackerProvider>
      <SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Cube.3mf' }} onClose={() => undefined} />
    </SliceJobTrackerProvider>,
  );
}

async function picker(label: string | RegExp): Promise<HTMLSelectElement> {
  return waitFor(() => screen.getByLabelText(label)) as Promise<HTMLSelectElement>;
}

/** The presets a picker offers — its options minus the placeholder. */
const offered = (select: HTMLSelectElement) =>
  Array.from(select.querySelectorAll('option'))
    .filter((o) => o.value !== '')
    .map((o) => o.textContent);

const onlineToggle = () => screen.findByRole('checkbox', { name: 'Only printers that are online' });
const loadedToggle = () => screen.findByRole('checkbox', { name: 'Only filaments that are loaded' });

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe('SliceModal — the farm filters start off', () => {
  it('lists every preset and asks nothing about the farm', async () => {
    serve();
    open();

    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(4));
    expect(await onlineToggle()).not.toBeChecked();
    expect(await loadedToggle()).not.toBeChecked();
    expect(farmRequests).toBe(0);
  });
});

describe('SliceModal — only printers that are online', () => {
  it('keeps the presets of an online model, a renamed one through the preset it was saved from', async () => {
    serve();
    open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(4));
    await userEvent.selectOptions(printer, `standard:${H2D}`);

    await userEvent.click(await onlineToggle());

    await waitFor(() => expect(offered(printer).sort()).toEqual([H2D, 'Garage machine'].sort()));
  });

  it('keeps and shows the chosen preset when the filter would hide it', async () => {
    serve();
    open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(4));
    await userEvent.selectOptions(printer, `standard:${A1}`);

    await userEvent.click(await onlineToggle());

    await waitFor(() => expect(offered(printer)).not.toContain(X1C));
    expect(offered(printer)).toContain(A1);
    expect(printer.value).toBe(`standard:${A1}`);
  });

  it('is remembered in this browser', async () => {
    serve();
    const first = open();
    await userEvent.click(await onlineToggle());
    await waitFor(() => expect(localStorage.getItem(ONLINE_KEY)).toBe('1'));
    first.unmount();

    open();

    expect(await onlineToggle()).toBeChecked();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).not.toContain(X1C));
    expect(localStorage.getItem(LOADED_KEY)).not.toBe('1');
  });

  it('says why the list is empty and shows everything again in one click', async () => {
    serve({ printers: [], families: {} });
    open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(4));
    await userEvent.selectOptions(printer, '');

    await userEvent.click(await onlineToggle());

    const note = await screen.findByText('No printer is online, so no printer profile matches.');
    await waitFor(() => expect(offered(printer)).toHaveLength(0));
    await userEvent.click(screen.getByRole('button', { name: 'Show all printer profiles' }));

    await waitFor(() => expect(offered(printer)).toHaveLength(4));
    expect(await onlineToggle()).not.toBeChecked();
    expect(note).not.toBeInTheDocument();
    expect(localStorage.getItem(ONLINE_KEY)).toBe('0');
  });

  it('filters nothing when the farm cannot be read, and says so', async () => {
    serve('error');
    open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(4));

    await userEvent.click(await onlineToggle());

    await screen.findByText('The printers could not be read, so every profile is shown.');
    expect(offered(printer)).toHaveLength(4);
  });
});

describe('SliceModal — only filaments that are loaded', () => {
  it('keeps the presets of the loaded family and the chosen one', async () => {
    serve();
    open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(4));
    await userEvent.selectOptions(printer, `standard:${H2D}`);
    const filament = await picker('Filament profile');
    await waitFor(() => expect(offered(filament)).toHaveLength(3));
    await userEvent.selectOptions(filament, 'standard:Generic PETG @BBL H2D');

    await userEvent.click(await loadedToggle());

    await waitFor(() =>
      expect(offered(filament).sort()).toEqual(['Bambu PLA Basic @BBL H2D', 'Generic PETG @BBL H2D'].sort()),
    );
    expect(filament.value).toBe('standard:Generic PETG @BBL H2D');
    await waitFor(() => expect(localStorage.getItem(LOADED_KEY)).toBe('1'));
  });

  it('counts only the online printers of the chosen printer’s model, and says when none of that model is online', async () => {
    serve();
    open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(4));
    // an A1 preset: the only online printer is an H2D, so nothing is loaded "there"
    await userEvent.selectOptions(printer, `standard:${A1}`);
    const filament = await picker('Filament profile');
    await userEvent.selectOptions(filament, '');

    await userEvent.click(await loadedToggle());

    // Something IS loaded on the farm (the H2D): the reason is the model.
    await screen.findByText('No printer of the chosen model is online, so no filament counts as loaded.');
    await waitFor(() => expect(offered(filament)).toHaveLength(0));
    await userEvent.click(screen.getByRole('button', { name: 'Show all filament profiles' }));
    await waitFor(() => expect(offered(filament)).toHaveLength(3));
    expect(await loadedToggle()).not.toBeChecked();
  });
});
