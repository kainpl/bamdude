/**
 * The calibration preset page on the shared slicing section (plan r6 Task 17):
 * the section decides the presets, the slicer and the plate; the page keeps
 * the slot, the nozzle and everything K exactly as it was.
 */
import { Profiler, useLayoutEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import pageSource from '../../../components/calibration/CalibrationPresetPage.tsx?raw';
import { api, type UnifiedPreset, type UnifiedPresetsResponse } from '../../../api/client';
import { CalibrationPresetPage } from '../../../components/calibration/CalibrationPresetPage';
import { usePresetCatalogue } from '../../../components/slicing-setup/usePresetCatalogue';
import { __resetSharedMemoryForTests } from '../../../components/slicing-setup/memory';
import { __resetPresetContextForTests, bumpPresetContext } from '../../../utils/presetContext';

const auth = { permissions: new Set<string>() };
vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1 }, hasPermission: (p: string) => auth.permissions.has(p) }),
}));

const A1_04 = 'Bambu Lab A1 0.4 nozzle';
const A1_PROCESS = '0.20mm Standard @BBL A1';
const MATTE = 'Bambu PLA Matte @BBL A1';
const none = { printer: [], process: [], filament: [] };
const std = (name: string, extra: Partial<UnifiedPreset> = {}) => ({ id: name, name, source: 'standard', ...extra }) as UnifiedPreset;
const LISTING = {
  local: none,
  orca_cloud: none,
  cloud: none,
  standard: {
    printer: [std(A1_04)],
    process: [std(A1_PROCESS, { compatible_printers: [A1_04] })],
    filament: [std('Bambu PLA Basic @BBL A1', { filament_type: 'PLA' }), std(MATTE, { filament_type: 'PLA' })],
  },
  cloud_status: 'ok',
  orca_cloud_status: 'ok',
} as unknown as UnifiedPresetsResponse;

function spies(model = 'A1') {
  vi.spyOn(api, 'getSlicerPresets').mockResolvedValue(LISTING);
  vi.spyOn(api, 'getPrinter').mockResolvedValue({ id: 1, model } as never);
  vi.spyOn(api, 'getPrinterModels').mockResolvedValue({ 'Bambu Lab A1': 'A1' });
  vi.spyOn(api, 'getSlicerHealth').mockResolvedValue({ healthy: false, version: null, error: 'offline', url: null } as never);
  vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ preferred_slicer: 'bambu_studio' } as never);
  vi.spyOn(api, 'listSlicerPipelines').mockResolvedValue({ pipelines: [] } as never);
  vi.spyOn(api, 'getCloudStatus').mockResolvedValue({ is_authenticated: false } as never);
  vi.spyOn(api, 'orcaCloudStatus').mockResolvedValue({ connected: false } as never);
  vi.spyOn(api, 'getSlicerLoadedFilaments').mockResolvedValue({
    printers: [],
    farm_printers: [],
    families: { GFA01: { name: 'Bambu PLA Matte', cloud: [], orca_cloud: [], local: [], standard: [MATTE], cloud_aliases: [] } },
  });
  vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({
    ams: [{ id: 0, tray: [{ id: 0, state: 11, tray_info_idx: 'GFA01', tray_type: 'PLA', tray_sub_brands: 'PLA Matte' }] }],
    vt_tray: [],
  } as never);
  vi.spyOn(api, 'getPrintOptionsPreference').mockResolvedValue(null);
}

function mount(onStart = vi.fn<(preset: unknown) => Promise<void>>(async () => {}), caliMode = 'pa_tower') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CalibrationPresetPage
        printerId={1}
        caliMode={caliMode as never}
        method="manual"
        capabilities={{ nozzles: [{ diameter: 0.4 }], extruders: [{ id: 0, name: 'Main' }], dual_extruder: false } as never}
        onBack={() => {}}
        onStart={onStart}
      />
    </QueryClientProvider>,
  );
  return onStart;
}
const startButton = () => screen.getByRole('button', { name: /start calibration/i });

beforeEach(() => {
  localStorage.clear();
  __resetSharedMemoryForTests();
  __resetPresetContextForTests();
  auth.permissions = new Set(['library:upload', 'printers:read', 'pipelines:read']);
});
afterEach(() => vi.restoreAllMocks());

describe('CalibrationPresetPage', () => {
  it('composes the shared section and keeps no picker of its own', () => {
    expect(pageSource).toContain('<CalibrationSlicingSection');
    expect(pageSource).not.toContain('function pickDefaultRef');
    expect(pageSource).not.toContain('function matchesPrinterModel');
    expect(pageSource).not.toContain("from './preset-picker/");
  });

  it('Start sends the section’s refs, slicer and plate; the chosen filament is still the setting id', async () => {
    spies();
    const onStart = mount();
    await userEvent.setup().click(await screen.findByRole('button', { name: /AMS 1 · Slot 1 · PLA Matte/ }));
    await waitFor(() => expect(startButton()).toBeEnabled());
    await userEvent.setup().click(startButton());
    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(1));
    const sent = onStart.mock.calls[0][0] as Record<string, unknown>;
    expect(sent.printer_preset).toEqual({ source: 'standard', id: A1_04 });
    expect(sent.process_preset).toEqual({ source: 'standard', id: A1_PROCESS });
    expect(sent.filament_presets).toEqual([{ source: 'standard', id: MATTE }]);
    expect(sent.slicer).toBe('bambu_studio');
    expect(sent.bed_type).toBe('Textured PEI Plate');
    expect((sent.filaments as { filament_setting_id: string }[])[0].filament_setting_id).toBe(MATTE);
  });

  it('Start waits while the section blocks it: no proven printer profile for this printer', async () => {
    spies('X1C');
    mount();
    await userEvent.setup().click(await screen.findByRole('button', { name: /AMS 1 · Slot 1 · PLA Matte/ }));
    await screen.findByLabelText('Printer profile');
    await new Promise((r) => setTimeout(r, 50));
    expect(startButton()).toBeDisabled();
  });

  it('Start waits for the new context’s catalogue and sends nothing of the old one (review I-2)', async () => {
    spies();
    const onStart = mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /AMS 1 · Slot 1 · PLA Matte/ }));
    await waitFor(() => expect(startButton()).toBeEnabled());
    vi.mocked(api.getSlicerPresets).mockImplementation(() => new Promise<UnifiedPresetsResponse>(() => {}));
    act(() => bumpPresetContext());
    await waitFor(() => expect(api.getSlicerPresets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(startButton()).toBeDisabled());
    await user.click(startButton());
    expect(onStart).not.toHaveBeenCalled();
  });

  it.each([
    ['pa_tower', 'PA Tower parameters'],
    ['pa_line', 'PA Line parameters'],
    ['pa_pattern', 'PA Pattern parameters'],
  ])('the %s parameters carry their own mode in the heading', async (mode, heading) => {
    spies();
    mount(undefined, mode);
    expect(await screen.findByText(heading)).toBeInTheDocument();
    if (mode !== 'pa_tower') expect(screen.queryByText('PA Tower parameters')).toBeNull();
  });

  it('the first commit without the new catalogue neither enables Start nor starts (review I-2)', async () => {
    spies();
    const onStart = vi.fn<(preset: unknown) => Promise<void>>(async () => {});
    const commitsWithoutCatalogue: boolean[] = [];
    let armed = false;
    function ObserveCommit() {
      const catalogue = usePresetCatalogue();
      useLayoutEffect(() => {
        if (!armed || catalogue.data !== undefined) return;
        const button = startButton() as HTMLButtonElement;
        commitsWithoutCatalogue.push(button.disabled);
        if (commitsWithoutCatalogue.length === 1 && !button.disabled) button.click();
      });
      return (
        <CalibrationPresetPage
          printerId={1}
          caliMode="pa_tower"
          method="manual"
          capabilities={{ nozzles: [{ diameter: 0.4 }], extruders: [{ id: 0, name: 'Main' }], dual_extruder: false } as never}
          onBack={() => {}}
          onStart={onStart}
        />
      );
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ObserveCommit />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /AMS 1 · Slot 1 · PLA Matte/ }));
    await waitFor(() => expect(startButton()).toBeEnabled());
    vi.mocked(api.getSlicerPresets).mockImplementation(() => new Promise<UnifiedPresetsResponse>(() => {}));
    armed = true;
    act(() => bumpPresetContext());
    await waitFor(() => expect(startButton()).toBeDisabled());
    expect(commitsWithoutCatalogue).not.toHaveLength(0);
    expect({ firstDisabled: commitsWithoutCatalogue[0], starts: onStart.mock.calls.length }).toEqual({ firstDisabled: true, starts: 0 });
  });

  // Review I-2 (third round): after a slot switch the section's filament is
  // reset in an effect, a commit after the slot key moved. No commit in
  // between may start the NEW slot with the OLD slot's preset — whether the
  // new slot's family has no preset or another one.
  const switchSlotsProbe = async (slotTwoTray: object, families: Record<string, object>) => {
    spies();
    vi.mocked(api.getPrinterStatus).mockResolvedValue({
      ams: [{ id: 0, tray: [{ id: 0, state: 11, tray_info_idx: 'GFA01', tray_type: 'PLA', tray_sub_brands: 'PLA Matte' }, slotTwoTray] }],
      vt_tray: [],
    } as never);
    vi.mocked(api.getSlicerLoadedFilaments).mockResolvedValue({ printers: [], farm_printers: [], families } as never);
    const onStart = vi.fn<(preset: unknown) => Promise<void>>(async () => {});
    let armed = false;
    let clicked = false;
    const slotTwo = () => screen.getByRole('button', { name: /AMS 1 · Slot 2/ });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Profiler
          id="calibration"
          onRender={() => {
            if (!armed || clicked || !slotTwo().classList.contains('border-bambu-green')) return;
            const start = startButton() as HTMLButtonElement;
            if (!start.disabled) {
              clicked = true;
              start.click();
            }
          }}
        >
          <CalibrationPresetPage
            printerId={1}
            caliMode="pa_tower"
            method="manual"
            capabilities={{ nozzles: [{ diameter: 0.4 }], extruders: [{ id: 0, name: 'Main' }], dual_extruder: false } as never}
            onBack={() => {}}
            onStart={onStart}
          />
        </Profiler>
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /AMS 1 · Slot 1 · PLA Matte/ }));
    await waitFor(() => expect(startButton()).toBeEnabled());
    armed = true;
    await user.click(slotTwo());
    await new Promise((r) => setTimeout(r, 100));
    return onStart.mock.calls.map(([preset]) => {
      const payload = preset as { filaments: { slot_id: number }[]; filament_presets: { id: string }[] };
      return { slot: payload.filaments[0].slot_id, preset: payload.filament_presets[0].id };
    });
  };
  const MATTE_FAMILY = { name: 'Bambu PLA Matte', cloud: [], orca_cloud: [], local: [], standard: [MATTE], cloud_aliases: [] };

  it('a switch to a slot whose family has no preset never starts with the previous slot’s preset (review I-2)', async () => {
    const requests = await switchSlotsProbe(
      { id: 1, state: 11, tray_info_idx: 'GFA02', tray_type: 'PLA', tray_sub_brands: 'PLA Unknown' },
      { GFA01: MATTE_FAMILY },
    );
    expect(requests).toEqual([]);
    expect(startButton()).toBeDisabled();
  });

  it('a switch to a slot of another family starts only with that slot’s own preset (review I-2)', async () => {
    const requests = await switchSlotsProbe(
      { id: 1, state: 11, tray_info_idx: 'GFA00', tray_type: 'PLA', tray_sub_brands: 'PLA Basic' },
      {
        GFA01: MATTE_FAMILY,
        GFA00: { name: 'Bambu PLA Basic', cloud: [], orca_cloud: [], local: [], standard: ['Bambu PLA Basic @BBL A1'], cloud_aliases: [] },
      },
    );
    // Whatever commit the click landed in, slot 2 never went with slot 1's Matte.
    expect(requests.filter((r) => r.slot === 1 && r.preset === MATTE)).toEqual([]);
    expect(requests.every((r) => r.slot === 1 && r.preset === 'Bambu PLA Basic @BBL A1')).toBe(true);
  });
});
