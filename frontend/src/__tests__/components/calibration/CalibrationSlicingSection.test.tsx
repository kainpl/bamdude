/**
 * The calibration slicing section (plan r6 Task 17, spec slicing-setup r5
 * §7.2, R4, R5, D6, D7): one section for the preset page and the verify page.
 */
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import {
  api,
  ApiError,
  type FamilyPresetRefs,
  type LoadedFilamentsResponse,
  type SlicerPipeline,
  type UnifiedPreset,
  type UnifiedPresetsResponse,
} from '../../../api/client';
import {
  CalibrationSlicingSection,
  type CalibrationPresetChoice,
  type CalibrationSlot,
} from '../../../components/calibration/CalibrationSlicingSection';
import { EMPTY_CALIBRATION_CHOICE } from '../../../components/calibration/calibrationChoice';
import { __resetSharedMemoryForTests } from '../../../components/slicing-setup/memory';
import { __resetPresetContextForTests } from '../../../utils/presetContext';
import { pickFamilyFilament } from '../../../utils/slicePresetPicker';
import { EMPTY_COMPATIBILITY_INDEX } from '../../../utils/slicerPrinterMatch';

const auth = { permissions: new Set<string>() };
vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1 }, hasPermission: (p: string) => auth.permissions.has(p) }),
}));

const A1_04 = 'Bambu Lab A1 0.4 nozzle';
const A1_06 = 'Bambu Lab A1 0.6 nozzle';
const MINI_04 = 'Bambu Lab A1 mini 0.4 nozzle';
const CARD = 'Bambu Lab A1';
const A1_PROCESS = '0.20mm Standard @BBL A1';
const X1C_PROCESS = '0.20mm Standard @BBL X1C';
const BASIC = 'Bambu PLA Basic @BBL A1';
const MATTE = 'Bambu PLA Matte @BBL A1';
const GENERIC = 'Generic PLA @BBL A1';

const none = { printer: [], process: [], filament: [] };
const std = (name: string, extra: Partial<UnifiedPreset> = {}) => ({ id: name, name, source: 'standard', ...extra }) as UnifiedPreset;
function listing(over: { process?: UnifiedPreset[]; printer?: string[] } = {}): UnifiedPresetsResponse {
  return {
    local: none,
    orca_cloud: none,
    cloud: none,
    standard: {
      printer: (over.printer ?? [CARD, MINI_04, A1_04, A1_06]).map((n) => std(n)),
      process: over.process ?? [
        std(A1_PROCESS, { compatible_printers: [A1_04] }),
        std(X1C_PROCESS, { compatible_printers: ['Bambu Lab X1 Carbon 0.4 nozzle'] }),
      ],
      filament: [std(BASIC, { filament_type: 'PLA' }), std(MATTE, { filament_type: 'PLA' }), std(GENERIC, { filament_type: 'PLA' })],
    },
    cloud_status: 'ok',
    orca_cloud_status: 'ok',
  } as unknown as UnifiedPresetsResponse;
}
const refs = (standard: string[], over: Partial<FamilyPresetRefs> = {}): FamilyPresetRefs => ({
  name: 'x',
  cloud: [],
  orca_cloud: [],
  local: [],
  standard,
  cloud_aliases: [],
  ...over,
});
const FARM: LoadedFilamentsResponse = {
  printers: [],
  farm_printers: [],
  families: { GFA01: refs([MATTE]) },
};
const SLOT_A: CalibrationSlot = { key: '0:0:0', label: 'AMS 1 · Slot 1 · PLA Matte', familyId: 'GFA01' };
const SLOT_B: CalibrationSlot = { key: '0:0:1', label: 'AMS 1 · Slot 2 · PLA Matte', familyId: 'GFA01' };

let latest: CalibrationPresetChoice = EMPTY_CALIBRATION_CHOICE;
type SectionProps = Partial<Parameters<typeof CalibrationSlicingSection>[0]>;
function Harness(props: SectionProps) {
  const [value, setValue] = useState<CalibrationPresetChoice>(EMPTY_CALIBRATION_CHOICE);
  return (
    <CalibrationSlicingSection
      printerId={1}
      mode="preset"
      needsPresetPicker
      nozzle={0.4}
      slot={SLOT_A}
      value={value}
      onChange={(v) => {
        latest = v;
        setValue(v);
      }}
      {...props}
    />
  );
}
function mount(props: SectionProps = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
  return {
    ...view,
    client,
    rerenderWith: (next: SectionProps) =>
      view.rerender(
        <QueryClientProvider client={client}>
          <Harness {...props} {...next} />
        </QueryClientProvider>,
      ),
  };
}
const field = (label: string) => waitFor(() => screen.getByLabelText(label)) as Promise<HTMLSelectElement>;
const options = (select: HTMLSelectElement) => Array.from(select.options).map((o) => o.value).filter(Boolean);

function spies(opts: { presets?: UnifiedPresetsResponse | Promise<UnifiedPresetsResponse>; farm?: LoadedFilamentsResponse | Promise<LoadedFilamentsResponse> | Error; model?: string; pipelines?: SlicerPipeline[] } = {}) {
  vi.spyOn(api, 'getSlicerPresets').mockImplementation(() => Promise.resolve(opts.presets ?? listing()));
  vi.spyOn(api, 'getPrinter').mockResolvedValue({ id: 1, model: opts.model ?? 'A1' } as never);
  vi.spyOn(api, 'getPrinterModels').mockResolvedValue({ 'Bambu Lab A1': 'A1', 'Bambu Lab A1 mini': 'A1M', 'Bambu Lab X1 Carbon': 'X1C' });
  vi.spyOn(api, 'getSlicerHealth').mockResolvedValue({ healthy: false, version: null, error: 'offline', url: null } as never);
  vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ preferred_slicer: 'bambu_studio' } as never);
  vi.spyOn(api, 'listSlicerPipelines').mockResolvedValue({ pipelines: opts.pipelines ?? [] } as never);
  vi.spyOn(api, 'getCloudStatus').mockResolvedValue({ is_authenticated: false } as never);
  vi.spyOn(api, 'orcaCloudStatus').mockResolvedValue({ connected: false } as never);
  const farm = opts.farm ?? FARM;
  return vi
    .spyOn(api, 'getSlicerLoadedFilaments')
    .mockImplementation(() => (farm instanceof Error ? Promise.reject(farm) : Promise.resolve(farm)));
}
const deferred = <T,>() => {
  let resolve: (v: T) => void = () => {};
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
};

beforeEach(() => {
  localStorage.clear();
  __resetSharedMemoryForTests();
  __resetPresetContextForTests();
  latest = EMPTY_CALIBRATION_CHOICE;
  auth.permissions = new Set(['library:upload', 'printers:read', 'pipelines:read']);
});
afterEach(() => vi.restoreAllMocks());

describe('CalibrationSlicingSection — preset mode', () => {
  it('proven bound: an A1 page never offers A1 mini; a bare card is not proven', async () => {
    spies();
    mount();
    const printer = await field('Printer profile');
    await waitFor(() => expect(options(printer)).toEqual([`standard:${A1_04}`]));
    await waitFor(() => expect(latest.printer).toEqual({ source: 'standard', id: A1_04 }));
  });

  it('no proven preset: the whole list, a non-proven pick blocks Start with a note', async () => {
    spies({ model: 'X1C' });
    mount();
    const printer = await field('Printer profile');
    await waitFor(() => expect(options(printer)).toHaveLength(4));
    expect(latest.printer).toBeNull();
    await userEvent.setup().selectOptions(printer, `standard:${A1_04}`);
    await waitFor(() => expect(latest.startBlocked).toBe('notProven'));
    expect(printer).toHaveAccessibleDescription(/not known to be this printer/);
  });

  it('family pick fills after the answer in either order; a person’s ref set first is kept', async () => {
    // catalogue → slot → answer
    const late = deferred<LoadedFilamentsResponse>();
    spies({ farm: late.promise });
    const first = mount();
    await waitFor(() => expect(latest.process).not.toBeNull());
    expect(latest.filament).toBeNull();
    expect(latest.familyHint).toBe('pending');
    late.resolve(FARM);
    await waitFor(() => expect(latest.filament).toEqual({ source: 'standard', id: MATTE }));
    first.unmount();
    vi.restoreAllMocks();

    // answer → catalogue
    const slow = deferred<UnifiedPresetsResponse>();
    spies({ presets: slow.promise });
    __resetPresetContextForTests();
    const second = mount();
    await new Promise((r) => setTimeout(r, 30));
    slow.resolve(listing());
    await waitFor(() => expect(latest.filament).toEqual({ source: 'standard', id: MATTE }));
    second.unmount();
    vi.restoreAllMocks();

    // a person's ref before the answer
    const pending = deferred<LoadedFilamentsResponse>();
    spies({ farm: pending.promise });
    __resetPresetContextForTests();
    mount();
    const filament = await field('Filament profile');
    await waitFor(() => expect(options(filament).length).toBeGreaterThan(0));
    await userEvent.setup().selectOptions(filament, `standard:${GENERIC}`);
    pending.resolve(FARM);
    await new Promise((r) => setTimeout(r, 30));
    expect(latest.filament).toEqual({ source: 'standard', id: GENERIC });
  });

  it('while the family answer is pending, a person’s filament allows Start', async () => {
    spies({ farm: new Promise(() => {}) });
    mount();
    const filament = await field('Filament profile');
    await waitFor(() => expect(latest.familyHint).toBe('pending'));
    expect(screen.getByText(/AMS 1 · Slot 1 · PLA Matte/)).toBeInTheDocument();
    await waitFor(() => expect(options(filament).length).toBeGreaterThan(0));
    await userEvent.setup().selectOptions(filament, `standard:${BASIC}`);
    await waitFor(() => expect(latest.startBlocked).toBe(false));
  });

  it('no farm request in an Auto method, nor in verify mode', async () => {
    const auto = spies();
    const one = mount({ needsPresetPicker: false });
    await field('Printer profile');
    await new Promise((r) => setTimeout(r, 30));
    expect(auto).not.toHaveBeenCalled();
    one.unmount();
    vi.restoreAllMocks();
    const verify = spies();
    mount({ mode: 'verify' });
    await field('Printer profile');
    await new Promise((r) => setTimeout(r, 30));
    expect(verify).not.toHaveBeenCalled();
  });

  it('a catalogue of process mismatches only: no process pick, Start blocked as empty', async () => {
    spies({ presets: listing({ process: [std(X1C_PROCESS, { compatible_printers: ['Bambu Lab X1 Carbon 0.4 nozzle'] })] }) });
    mount();
    await waitFor(() => expect(latest.printer).toEqual({ source: 'standard', id: A1_04 }));
    await new Promise((r) => setTimeout(r, 30));
    expect(latest.process).toBeNull();
    expect(latest.startBlocked).toBe('empty');
  });

  // Review I-3: the farm read goes ready → error while another slot is chosen,
  // then answers again with an EQUAL body — TanStack keeps the same object.
  // The family pick must still run for the slot chosen meanwhile; a person's
  // choice made meanwhile stays.
  const loseAndRegainTheAnswer = async (view: ReturnType<typeof mount>, farmSpy: ReturnType<typeof spies>) => {
    const query = view.client
      .getQueryCache()
      .findAll({ queryKey: ['slicerLoadedFilaments'] })
      .find((q) => q.state.data !== undefined);
    const previous = query?.state.data;
    farmSpy.mockRejectedValueOnce(new Error('temporary farm failure'));
    await act(async () => {
      await view.client.invalidateQueries({ queryKey: ['slicerLoadedFilaments'] });
    });
    await waitFor(() => expect(query?.state.status).toBe('error'));
    return { query, previous };
  };

  it('the family pick recovers after an unchanged farm answer (review I-3)', async () => {
    const farmSpy = spies();
    const view = mount();
    await waitFor(() => expect(latest.filament).toEqual({ source: 'standard', id: MATTE }));
    const { query, previous } = await loseAndRegainTheAnswer(view, farmSpy);
    view.rerenderWith({ slot: SLOT_B });
    await waitFor(() => expect(latest.filament).toBeNull());
    farmSpy.mockResolvedValueOnce(structuredClone(FARM));
    await act(async () => {
      await view.client.invalidateQueries({ queryKey: ['slicerLoadedFilaments'] });
    });
    await waitFor(() => expect(query?.state.status).toBe('success'));
    expect(query?.state.data).toBe(previous);
    await waitFor(() => expect(latest.filament).toEqual({ source: 'standard', id: MATTE }));
  });

  it('a person’s choice made while the answer was lost stays when it returns (review I-3)', async () => {
    const farmSpy = spies();
    const view = mount();
    await waitFor(() => expect(latest.filament).toEqual({ source: 'standard', id: MATTE }));
    await loseAndRegainTheAnswer(view, farmSpy);
    view.rerenderWith({ slot: SLOT_B });
    await waitFor(() => expect(latest.filament).toBeNull());
    await userEvent.setup().selectOptions(await field('Filament profile'), `standard:${BASIC}`);
    await waitFor(() => expect(latest.filament?.id).toBe(BASIC));
    farmSpy.mockResolvedValueOnce(structuredClone(FARM));
    await act(async () => {
      await view.client.invalidateQueries({ queryKey: ['slicerLoadedFilaments'] });
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(latest.filament?.id).toBe(BASIC);
  });

  it('two slots of the same family: switching resets and re-picks by key', async () => {
    spies();
    const view = mount();
    await waitFor(() => expect(latest.filament).toEqual({ source: 'standard', id: MATTE }));
    await userEvent.setup().selectOptions(await field('Filament profile'), `standard:${GENERIC}`);
    await waitFor(() => expect(latest.filament?.id).toBe(GENERIC));
    view.rerenderWith({ slot: SLOT_B });
    await waitFor(() => expect(latest.filament).toEqual({ source: 'standard', id: MATTE }));
  });

  it('family absent, forbidden or failed: no pick, the slot label shown, manual pick possible', async () => {
    for (const farm of [{ ...FARM, families: {} }, new ApiError('no', 403), new ApiError('boom', 500)]) {
      vi.restoreAllMocks();
      __resetPresetContextForTests();
      spies({ farm });
      const view = mount();
      await waitFor(() => expect(latest.familyHint).toBe(farm instanceof Error ? 'unavailable' : 'absent'));
      expect(latest.filament).toBeNull();
      expect(screen.getByText(/AMS 1 · Slot 1 · PLA Matte/)).toBeInTheDocument();
      await userEvent.setup().selectOptions(await field('Filament profile'), `standard:${BASIC}`);
      await waitFor(() => expect(latest.filament?.id).toBe(BASIC));
      view.unmount();
    }
  });

  it('the by_material branch is never used', async () => {
    spies({
      farm: {
        printers: [{ printer_id: 1, model: 'A1', accepts: [], nozzle_diameters: [0.4], filaments: [{ kind: 'ams', material: 'PLA', family_id: null, by_material: true }] }],
        farm_printers: [],
        families: {},
      },
    });
    mount({ slot: { key: '0:0:0', label: 'AMS 1 · Slot 1 · PLA', familyId: 'GFL99' } });
    await waitFor(() => expect(latest.familyHint).toBe('absent'));
    expect(latest.filament).toBeNull();
  });

  it('nozzle change keeps a person’s printer, marked, Start blocked', async () => {
    spies();
    const view = mount();
    const printer = await field('Printer profile');
    await waitFor(() => expect(latest.printer?.id).toBe(A1_04));
    await userEvent.setup().selectOptions(printer, `standard:${A1_04}`);
    view.rerenderWith({ nozzle: 0.6 });
    await waitFor(() => expect(latest.startBlocked).toBe('notProven'));
    expect(latest.printer).toEqual({ source: 'standard', id: A1_04 });
    expect(await field('Printer profile')).toHaveAccessibleDescription(/not known to be this printer/);
  });

  it('pipelines listed only when proven; filament untouched', async () => {
    const set = (id: number, printer: string): SlicerPipeline =>
      ({
        id,
        name: `set ${id}`,
        printer_preset: { source: 'standard', id: printer },
        process_preset: { source: 'standard', id: A1_PROCESS },
        filament_presets: [{ source: 'standard', id: GENERIC }],
        bed_type: 'Cool Plate',
      }) as SlicerPipeline;
    spies({ pipelines: [set(1, A1_04), set(2, MINI_04)] });
    mount();
    await waitFor(() => expect(latest.filament?.id).toBe(MATTE));
    const sets = await field('Saved settings');
    await waitFor(() => expect(options(sets)).toEqual(['1']));
    await userEvent.setup().selectOptions(sets, '1');
    await waitFor(() => expect(latest.plate).toBe('Cool Plate'));
    expect(latest.filament?.id).toBe(MATTE);
  });

  it('without library:upload: a sentence, Start blocked', async () => {
    auth.permissions = new Set(['printers:read']);
    spies();
    mount();
    expect(await screen.findByText(/needs the right to slice/i)).toBeInTheDocument();
    await waitFor(() => expect(latest.startBlocked).toBe('noCatalogue'));
  });
});

describe('CalibrationSlicingSection — verify mode', () => {
  it('verify mode: another model allowed and marked; filament first fitting; pipelines fill slot 0', async () => {
    spies({
      model: 'A1 mini',
      pipelines: [
        {
          id: 3,
          name: 'set 3',
          printer_preset: { source: 'standard', id: A1_04 },
          process_preset: { source: 'standard', id: A1_PROCESS },
          filament_presets: [{ source: 'standard', id: GENERIC }],
          bed_type: null,
        } as unknown as SlicerPipeline,
      ],
    });
    mount({ mode: 'verify', slot: null });
    const printer = await field('Printer profile');
    await waitFor(() => expect(options(printer)).toHaveLength(4));
    // An A1 mini on the page; an A1 profile is allowed, and marked.
    await userEvent.setup().selectOptions(printer, `standard:${A1_04}`);
    await waitFor(() => expect(printer).toHaveAccessibleDescription(/another model/));
    expect(latest.startBlocked).not.toBe('notProven');
    await waitFor(() => expect(latest.filament?.id).toBe(BASIC));
    await userEvent.setup().selectOptions(await field('Saved settings'), '3');
    await waitFor(() => expect(latest.filament?.id).toBe(GENERIC));
  });
});

describe('pickFamilyFilament — Task 14’s fixtures through the real wire', () => {
  const cloud = (id: string, type = 'TPU') => ({ id, name: id, source: 'cloud', filament_type: type }) as UnifiedPreset;
  const by = (ids: string[]) =>
    ({ ...listing(), standard: none, cloud: { ...none, filament: ids.map((id) => cloud(id)) } }) as unknown as UnifiedPresetsResponse;
  const pick = (ids: string[], family: FamilyPresetRefs) => pickFamilyFilament(by(ids), family, null, EMPTY_COMPATIBILITY_INDEX);
  const TPU = refs([], { cloud: ['GFSR99', 'GFSR99_10'] });
  const EVA = refs([], { cloud: ['GFSR99_00', 'GFSR99_01'] });

  it('TPU / EVA both ways', () => {
    expect(pick(['GFSR99_01', 'GFSR99_10'], TPU)).toEqual({ source: 'cloud', id: 'GFSR99_10' });
    expect(pick(['GFSR99', 'GFSR99_10'], EVA)).toBeNull();
  });
  it('PP / PP-GF', () => {
    expect(pick(['GFSP97_03'], refs([], { cloud: ['GFSP97_00'] }))).toBeNull();
    expect(pick(['GFSP97_00'], refs([], { cloud: ['GFSP97_03'] }))).toBeNull();
  });
  it('the alias GFSA06 and an unknown suffixed variant', () => {
    const lite = refs([], { cloud: ['GFSA06_00'], cloud_aliases: ['GFSA06'] });
    expect(pick(['GFSA06'], lite)).toEqual({ source: 'cloud', id: 'GFSA06' });
    expect(pick(['GFSA06_77'], lite)).toEqual({ source: 'cloud', id: 'GFSA06_77' });
    expect(pick(['GFSA060'], lite)).toBeNull();
  });
  it('no refs, no pick', () => expect(pick(['GFSA06'], undefined as unknown as FamilyPresetRefs)).toBeNull());
});
