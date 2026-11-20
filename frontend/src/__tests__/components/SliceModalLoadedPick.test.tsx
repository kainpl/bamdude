/**
 * "Only filaments that are loaded", beyond the lists: while the switch is on
 * and the farm can be read, the dialog's AUTOMATIC filament pick prefers a
 * preset of something that is loaded, and falls back to its ordinary pick when
 * nothing loaded suits the plate. A choice made by hand, and the filament an
 * assembled project names, are not its to replace.
 */
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router';
import { http, HttpResponse } from 'msw';

// For its side effect: it signs the test user in, without which `AuthProvider`
// loads nobody and every permission reads as missing.
import '../utils';
import { server } from '../mocks/server';
import { SliceModal } from '../../components/SliceModal';
import { AuthProvider } from '../../contexts/AuthContext';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { usePresetFilters } from '../../components/slicing-setup/usePresetFilters';
import { buildPrinterParents } from '../../utils/printerPresetParents';
import type { LoadedFilamentsResponse, UnifiedPresetsResponse } from '../../api/client';
import { __resetSharedMemoryForTests } from '../../components/slicing-setup/memory';

const H2D = 'Bambu Lab H2D 0.4 nozzle';
const BASIC = 'Bambu PLA Basic @BBL H2D';
const MATTE = 'Bambu PLA Matte @BBL H2D';
const GENERIC = 'Generic PLA @BBL H2D';
const PETG = 'Generic PETG @BBL H2D';
const LOADED_KEY = 'bamdude:slice-modal:only-loaded-filaments';

const farmWith = (family_id: string, material: string): LoadedFilamentsResponse => ({
  farm_printers: [],
  printers: [
    {
      printer_id: 1,
      model: 'H2D',
      accepts: [],
      nozzle_diameters: [0.4],
      filaments: [{ kind: 'ams', material, family_id, by_material: false }],
    },
  ],
  families: {
    GFA01: { name: 'Bambu PLA Matte', cloud: [], orca_cloud: [], local: [], standard: [MATTE], cloud_aliases: [] },
    GFG99: { name: 'Generic PETG', cloud: [], orca_cloud: [], local: [], standard: [PETG], cloud_aliases: [] },
  },
});
const MATTE_LOADED = farmWith('GFA01', 'PLA');
const PETG_LOADED = farmWith('GFG99', 'PETG');

const none = { printer: [], process: [], filament: [] };
const PRESETS = {
  local: none,
  orca_cloud: none,
  cloud: none,
  standard: {
    printer: [{ id: H2D, name: H2D, source: 'standard' }],
    process: [{ id: 'p', name: '0.20mm Standard @BBL H2D', source: 'standard' }],
    // The listing's first PLA is Basic: with nothing to tell them apart that is the ordinary pick.
    filament: [
      { id: BASIC, name: BASIC, source: 'standard', filament_type: 'PLA' },
      { id: MATTE, name: MATTE, source: 'standard', filament_type: 'PLA' },
      { id: GENERIC, name: GENERIC, source: 'standard', filament_type: 'PLA' },
      { id: PETG, name: PETG, source: 'standard', filament_type: 'PETG' },
    ],
  },
  cloud_status: 'ok',
  orca_cloud_status: 'ok',
};

// What the farm route answers next; a test swaps it while the dialog is open.
let farm: LoadedFilamentsResponse | number = MATTE_LOADED;

function serve(
  first: LoadedFilamentsResponse | number = MATTE_LOADED,
  embedded_filaments?: string[],
  presets: typeof PRESETS = PRESETS,
) {
  farm = first;
  server.use(
    http.get('*/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({
        plates: [{ index: 1, name: 'Plate 1' }],
        ...(embedded_filaments
          ? { embedded_printer: H2D, embedded_process: '0.20mm Standard @BBL H2D', embedded_filaments }
          : {}),
      }),
    ),
    http.get('*/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({
        file_id: 1,
        filename: 'Cube.3mf',
        filaments: [{ slot_id: 1, type: 'PLA', color: '#FFFFFF', used_grams: 1, used_meters: 1 }],
      }),
    ),
    http.get('*/api/v1/slicer/presets', () => HttpResponse.json(presets)),
    http.get('*/api/v1/slicer/printer-models', () =>
      HttpResponse.json({ 'Bambu Lab H2D': 'H2D', 'Bambu Lab A1': 'A1' }),
    ),
    http.get('*/api/v1/slicer/loaded-filaments', () =>
      typeof farm === 'number' ? HttpResponse.json({ detail: 'refused' }, { status: farm }) : HttpResponse.json(farm),
    ),
    http.get('*/api/v1/slicer/health/:kind', () => HttpResponse.json({ healthy: false, error: 'offline', url: null })),
    http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
    http.get('*/api/v1/slicer-pipelines/', () => HttpResponse.json({ pipelines: [] })),
  );
}

function providers() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const Providers = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <ThemeProvider>
          <AuthProvider>
            <ToastProvider>
              <SliceJobTrackerProvider>{children}</SliceJobTrackerProvider>
            </ToastProvider>
          </AuthProvider>
        </ThemeProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
  /** What a WebSocket event does in the app: the farm answer is read again. */
  const readFarmAgain = () => client.invalidateQueries({ queryKey: ['slicerLoadedFilaments'] });
  return { Providers, readFarmAgain };
}

function open() {
  const { Providers, readFarmAgain } = providers();
  render(<SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Cube.3mf' }} onClose={() => undefined} />, {
    wrapper: Providers,
  });
  return { readFarmAgain };
}

async function filamentPicker(): Promise<HTMLSelectElement> {
  return waitFor(() => screen.getByLabelText('Filament profile')) as Promise<HTMLSelectElement>;
}
const picked = async (name: string) => {
  const filament = await filamentPicker();
  await waitFor(() => expect(filament.value).toBe(`standard:${name}`));
  return filament;
};
const loadedToggle = () => screen.findByRole('checkbox', { name: 'Only filaments that are loaded' });

// Spec slicing-setup §4.4.1: the source filter's first value is Mine. These
// tests are about the rest of the dialog, so each starts from All.
const startFromAllSources = () => {
  __resetSharedMemoryForTests();
  localStorage.setItem('bamdude:presets:source-filter', 'all');
};
beforeEach(() => {
  localStorage.clear();
  startFromAllSources();
});
afterEach(() => localStorage.clear());

describe('SliceModal — the automatic filament pick and "only filaments that are loaded"', () => {
  it('prefers the loaded preset while the switch is on, and returns to the ordinary pick when it is turned off', async () => {
    const user = userEvent.setup();
    serve();
    open();
    await picked(BASIC);

    await user.click(await loadedToggle());
    await picked(MATTE);

    await user.click(await loadedToggle());
    await picked(BASIC);
  });

  it('the automatic loaded pick still takes the GFSA00_07 preset', async () => {
    // A Bambu Cloud system variant listed under an id newer than the catalogue:
    // the family names its base as an alias (spec slicing-setup r5 §8.3).
    const CLOUD_BASIC = { id: 'GFSA00_07', name: 'Bambu PLA Basic @BBL H2D (cloud)', source: 'cloud', filament_type: 'PLA' };
    // An own cloud PLA listed first: the ordinary pick takes it, so only the
    // alias can steer the pick to the loaded Basic.
    const OWN_PLA = { id: 'PFUSownpla', name: 'My PLA', source: 'cloud', filament_type: 'PLA' };
    const withCloud = { ...PRESETS, cloud: { ...none, filament: [OWN_PLA, CLOUD_BASIC] } } as typeof PRESETS;
    const basicFarm = (cloud_aliases: string[]): LoadedFilamentsResponse => ({
      ...farmWith('GFA00', 'PLA'),
      families: { GFA00: { name: 'Bambu PLA Basic', cloud: ['GFSA00'], orca_cloud: [], local: [], standard: [], cloud_aliases } },
    });
    localStorage.setItem(LOADED_KEY, '1');
    serve(basicFarm(['GFSA00']), undefined, withCloud);
    open();
    const filament = await filamentPicker();
    await waitFor(() => expect(filament.value).toBe('cloud:GFSA00_07'));
  });

  it('prefers the loaded preset when the dialog opens with the switch remembered on', async () => {
    localStorage.setItem(LOADED_KEY, '1');
    serve();
    open();

    await picked(MATTE);
  });

  it('follows a new farm answer', async () => {
    const user = userEvent.setup();
    serve(PETG_LOADED);
    const { readFarmAgain } = open();
    await picked(BASIC);
    await user.click(await loadedToggle());
    await picked(BASIC);

    farm = MATTE_LOADED;
    await readFarmAgain();

    await picked(MATTE);
  });

  it('falls back to the ordinary pick when nothing loaded is of the plate’s material', async () => {
    const user = userEvent.setup();
    serve(PETG_LOADED);
    open();
    const filament = await picked(BASIC);

    await user.click(await loadedToggle());

    // The list is narrowed to what is loaded (and the chosen preset)…
    await waitFor(() => expect(filament.querySelector(`option[value="standard:${MATTE}"]`)).toBeNull());
    // …and the PLA plate did not get the loaded PETG.
    expect(filament.value).toBe(`standard:${BASIC}`);
  });

  it('falls back when the only loaded preset is one for another printer', async () => {
    const MATTE_A1 = 'Bambu PLA Matte @BBL A1';
    const user = userEvent.setup();
    serve(
      {
        ...MATTE_LOADED,
        families: { GFA01: { name: 'Bambu PLA Matte', cloud: [], orca_cloud: [], local: [], standard: [MATTE_A1], cloud_aliases: [] } },
      },
      undefined,
      {
        ...PRESETS,
        standard: {
          ...PRESETS.standard,
          filament: [
            { id: BASIC, name: BASIC, source: 'standard', filament_type: 'PLA' },
            { id: MATTE_A1, name: MATTE_A1, source: 'standard', filament_type: 'PLA' },
          ],
        },
      },
    );
    open();
    const filament = await picked(BASIC);

    await user.click(await loadedToggle());

    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Only filaments that are loaded' })).toBeChecked());
    await waitFor(() => expect(localStorage.getItem(LOADED_KEY)).toBe('1'));
    expect(filament.value).toBe(`standard:${BASIC}`);
  });

  it('leaves a choice made by hand alone, on and off', async () => {
    const user = userEvent.setup();
    serve();
    open();
    const filament = await picked(BASIC);
    await user.selectOptions(filament, `standard:${GENERIC}`);

    await user.click(await loadedToggle());
    await waitFor(() => expect(filament.querySelector(`option[value="standard:${BASIC}"]`)).toBeNull());
    expect(filament.value).toBe(`standard:${GENERIC}`);

    await user.click(await loadedToggle());
    await waitFor(() => expect(filament.querySelector(`option[value="standard:${BASIC}"]`)).not.toBeNull());
    expect(filament.value).toBe(`standard:${GENERIC}`);
  });

  it('keeps the filament the project itself names', async () => {
    localStorage.setItem(LOADED_KEY, '1');
    serve(MATTE_LOADED, [GENERIC]);
    open();

    const filament = await picked(GENERIC);
    // The farm answer arrived (the list is narrowed) and did not replace it.
    await waitFor(() => expect(filament.querySelector(`option[value="standard:${BASIC}"]`)).toBeNull());
    expect(filament.value).toBe(`standard:${GENERIC}`);
  });

  it('is not steered by a farm that cannot be read', async () => {
    localStorage.setItem(LOADED_KEY, '1');
    serve(500);
    open();

    await screen.findByText(/could not be read/);
    await picked(BASIC);
  });

  it('is not steered by a refused read', async () => {
    localStorage.setItem(LOADED_KEY, '1');
    serve(403);
    open();

    await screen.findByText(/need the right to view printers/);
    await picked(BASIC);
  });
});

// The family's filter hook in the dialog's place (plan r6 Task 16): the
// loaded filter steers a pick only while its answer is good.
describe('usePresetFilters — what may steer a pick', () => {
  const presets = PRESETS as unknown as UnifiedPresetsResponse;
  const matte = presets.standard.filament.find((p) => p.name === MATTE)!;
  const ctx = { slot: 'filament' as const, printerName: H2D, parentsBySource: { standard: buildPrinterParents(presets, 'standard') } };
  const loaded = (state: ReturnType<typeof usePresetFilters>) => state.softFilters(ctx).find((f) => f.id === 'loaded');

  it('offers the loaded presets to the pick while the answer is good, and not once it is stale', async () => {
    localStorage.setItem(LOADED_KEY, '1');
    serve();
    const { Providers, readFarmAgain } = providers();
    const { result } = renderHook(() => usePresetFilters({ loaded: true }), { wrapper: Providers });
    await waitFor(() => expect(loaded(result.current)?.pickKeep?.(matte)).toBe(true));

    farm = 500;
    await readFarmAgain();

    await waitFor(() => expect(result.current.farmStatus).toBe('stale'));
    // The lists stay narrowed by the last good answer; a pick is not made from it.
    expect(loaded(result.current)?.keep(matte)).toBe(true);
    expect(loaded(result.current)?.pickKeep).toBeNull();
  });

  it('offers nothing to the pick while the switch is off', async () => {
    serve();
    const { Providers } = providers();
    const { result } = renderHook(() => usePresetFilters({ loaded: true }), { wrapper: Providers });

    expect(loaded(result.current)).toBeUndefined();
  });
});
