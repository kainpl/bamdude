/**
 * The Slice dialog's preset lists, rendered against a served listing.
 *
 * The matching rules themselves are unit-tested in `utils/`; these pin what
 * the dialog does with them.
 */
import { describe, expect, it } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { SliceModal } from '../../components/SliceModal';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import type { UnifiedPreset, UnifiedPresetsBySlot } from '../../api/client';

type Tier = 'local' | 'orca_cloud' | 'cloud' | 'standard';
type Listing = Partial<Record<Tier, Partial<Record<keyof UnifiedPresetsBySlot, Partial<UnifiedPreset>[]>>>>;

const H2D = 'Bambu Lab H2D 0.4 nozzle';
const A1 = 'Bambu Lab A1 0.4 nozzle';

function serve(listing: Listing, extra: Parameters<typeof server.use> = []) {
  const tier = (source: Tier) => {
    const of = (slot: keyof UnifiedPresetsBySlot) =>
      (listing[source]?.[slot] ?? []).map((p) => ({ id: p.name, source, ...p }));
    return { printer: of('printer'), process: of('process'), filament: of('filament') };
  };
  server.use(
    http.get('*/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] }),
    ),
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
    http.get('*/api/v1/slicer/health/:kind', () => HttpResponse.json({ healthy: false, error: 'offline', url: null })),
    http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
    http.get('*/api/v1/slicer/pipelines', () => HttpResponse.json({ pipelines: [] })),
    ...extra,
  );
}

function open() {
  return render(
    <SliceJobTrackerProvider>
      <SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Cube.3mf' }} onClose={() => undefined} />
    </SliceJobTrackerProvider>,
  );
}

/** The control labelled `label`, once the listing has rendered it. */
async function picker(label: string | RegExp): Promise<HTMLElement> {
  return waitFor(() => screen.getByLabelText(label));
}

describe('SliceModal — a printer preset saved under a name of its own', () => {
  const listing: Listing = {
    local: { printer: [{ name: 'Garage H2D', inherits: H2D }] },
    standard: {
      process: [
        { name: '0.20mm Standard @BBL A1', compatible_printers: [A1] },
        { name: '0.20mm Standard @BBL H2D', compatible_printers: [H2D] },
      ],
      filament: [
        { name: 'Bambu PLA Basic @BBL A1', filament_type: 'PLA', compatible_printers: [A1] },
        { name: 'Bambu PLA Basic @BBL H2D', filament_type: 'PLA', compatible_printers: [H2D] },
      ],
    },
  };

  it('pre-picks the process and filament of the preset it was saved from', async () => {
    serve(listing);
    open();

    const process = (await picker('Process profile')) as HTMLSelectElement;
    await waitFor(() => expect(process.value).toBe('standard:0.20mm Standard @BBL H2D'));
    const filament = (await picker('Filament profile')) as HTMLSelectElement;
    await waitFor(() => expect(filament.value).toBe('standard:Bambu PLA Basic @BBL H2D'));
  });
});

const groupsOf = (select: HTMLElement) =>
  Array.from(select.querySelectorAll('optgroup')).map((g) => [
    g.label,
    Array.from(g.querySelectorAll('option')).map((o) => o.textContent),
  ]);

describe('SliceModal — how a preset list is ordered', () => {
  const X1C = 'Bambu Lab X1 Carbon 0.4 nozzle';
  const listing: Listing = {
    local: {
      printer: [{ name: X1C }],
      process: [{ name: 'Zebra draft' }, { name: '0.20mm Mine' }, { name: '0.08mm Mine' }],
    },
    orca_cloud: { process: [{ name: 'Orca tuned', id: 'a-uuid' }] },
    cloud: {
      process: [
        { name: 'My cloud process', id: 'PPUS1234' },
        { name: '0.20mm Standard @BBL X1C', id: 'GP004' },
        { name: '0.20mm Standard @BBL A1', id: 'GP100' },
      ],
    },
    standard: {
      process: [
        { name: '0.20mm Standard @BBL X1C' },
        { name: '0.08mm Extra Fine @BBL X1C' },
        { name: '0.20mm Standard @BBL A1' },
      ],
      filament: [
        { name: 'Generic PLA', filament_type: 'PLA' },
        { name: 'Bambu PLA Basic', filament_type: 'PLA' },
      ],
    },
  };

  it("lists the user's own sources first, then the stock ones, each sorted", async () => {
    serve(listing);
    open();

    const process = await picker('Process profile');
    await waitFor(() => expect(process.querySelectorAll('optgroup').length).toBeGreaterThan(0));
    expect(groupsOf(process)).toEqual([
      ['Imported', ['0.08mm Mine', '0.20mm Mine', 'Zebra draft']],
      ['Orca Cloud', ['Orca tuned']],
      ['Bambu Cloud — my presets', ['My cloud process']],
      // The same stock preset, once per source: a name is not an identity,
      // and the two copies are different content.
      ['Bambu Cloud', ['0.20mm Standard @BBL X1C']],
      ['Standard', ['0.08mm Extra Fine @BBL X1C', '0.20mm Standard @BBL X1C']],
      // Presets of another printer say where each comes from.
      ['Other printers', ['0.20mm Standard @BBL A1 · Bambu Cloud', '0.20mm Standard @BBL A1 · Standard']],
    ]);
  });

  it('never lets the display order change what is pre-picked', async () => {
    serve(listing);
    open();

    // Listed Bambu-first; picked in the order the listing arrived, where the
    // two tie.
    const filament = (await picker('Filament profile')) as HTMLSelectElement;
    await waitFor(() => expect(filament.value).toBe('standard:Generic PLA'));
    expect(groupsOf(filament)).toEqual([['Standard', ['Bambu PLA Basic', 'Generic PLA']]]);
  });
});

describe('SliceModal — searching a preset list', () => {
  const X1C = 'Bambu Lab X1 Carbon 0.4 nozzle';
  const listing: Listing = {
    local: { printer: [{ name: X1C }] },
    standard: {
      process: [
        { name: '0.20mm Standard @BBL X1C' },
        { name: '0.08mm Extra Fine @BBL X1C' },
        { name: '0.12mm Fine @BBL A1' },
      ],
      filament: [{ name: 'Generic PLA', filament_type: 'PLA' }],
    },
  };

  it('narrows one list by the words typed, other printers included', async () => {
    const user = userEvent.setup();
    serve(listing);
    open();

    const process = (await picker('Process profile')) as HTMLSelectElement;
    await waitFor(() => expect(process.value).toBe('standard:0.20mm Standard @BBL X1C'));
    await user.click(screen.getByRole('button', { name: 'Search: Process profile' }));
    await user.type(screen.getByRole('searchbox', { name: 'Search: Process profile' }), 'fine');

    expect(groupsOf(process)).toEqual([
      // The chosen preset stays listed whatever is typed.
      ['Standard', ['0.08mm Extra Fine @BBL X1C', '0.20mm Standard @BBL X1C']],
      ['Other printers', ['0.12mm Fine @BBL A1 · Standard']],
    ]);
  });

  it('gives every preset list its own search', async () => {
    serve(listing);
    open();

    await picker('Filament profile');
    for (const list of ['Printer profile', 'Process profile', 'Filament profile']) {
      expect(screen.getByRole('button', { name: `Search: ${list}` })).toBeInTheDocument();
    }
  });
});
