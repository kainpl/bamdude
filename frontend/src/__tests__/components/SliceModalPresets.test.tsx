/**
 * The Slice dialog's preset lists, rendered against a served listing.
 *
 * The matching rules themselves are unit-tested in `utils/`; these pin what
 * the dialog does with them.
 */
import { describe, expect, it } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
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
