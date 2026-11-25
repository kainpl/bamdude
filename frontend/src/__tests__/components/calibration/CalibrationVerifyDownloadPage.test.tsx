/**
 * The calibration verify page on the shared slicing section (plan r6 Task 18):
 * the section decides the presets, the slicer and the plate; bake-only keeps
 * its own readiness and sends the slicer only once it is known (spec §7.2,
 * §8.2).
 */
import { useLayoutEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import pageSource from '../../../components/calibration/CalibrationVerifyDownloadPage.tsx?raw';
import { api, type CalibBakeOnlyIn, type UnifiedPreset, type UnifiedPresetsResponse } from '../../../api/client';
import { CalibrationVerifyDownloadPage } from '../../../components/calibration/CalibrationVerifyDownloadPage';
import { usePresetCatalogue } from '../../../components/slicing-setup/usePresetCatalogue';
import { ToastProvider } from '../../../contexts/ToastContext';
import { __resetSharedMemoryForTests } from '../../../components/slicing-setup/memory';
import { __resetPresetContextForTests, bumpPresetContext } from '../../../utils/presetContext';

const auth = { permissions: new Set<string>() };
vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1 }, hasPermission: (p: string) => auth.permissions.has(p) }),
}));

const none = { printer: [], process: [], filament: [] };
const std = (name: string, extra: Partial<UnifiedPreset> = {}) => ({ id: name, name, source: 'standard', ...extra }) as UnifiedPreset;
const LISTING = {
  local: none,
  orca_cloud: none,
  cloud: none,
  standard: {
    printer: [std('Bambu Lab A1 0.4 nozzle')],
    process: [std('0.20mm Standard @BBL A1', { compatible_printers: ['Bambu Lab A1 0.4 nozzle'] })],
    filament: [std('Bambu PLA Basic @BBL A1', { filament_type: 'PLA' })],
  },
  cloud_status: 'ok',
  orca_cloud_status: 'ok',
} as unknown as UnifiedPresetsResponse;

function spies(health: { orca: boolean; bambu: boolean } | 'pending') {
  vi.spyOn(api, 'getSlicerPresets').mockResolvedValue(LISTING);
  vi.spyOn(api, 'getPrinter').mockResolvedValue({ id: 1, model: 'A1' } as never);
  vi.spyOn(api, 'getPrinterModels').mockResolvedValue({ 'Bambu Lab A1': 'A1' });
  vi.spyOn(api, 'getSlicerHealth').mockImplementation((kind) =>
    health === 'pending'
      ? new Promise(() => {})
      : Promise.resolve({
          healthy: kind === 'orcaslicer' ? health.orca : health.bambu,
          version: '2.3',
          error: null,
          url: null,
        } as never),
  );
  vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ preferred_slicer: 'bambu_studio' } as never);
  vi.spyOn(api, 'listSlicerPipelines').mockResolvedValue({ pipelines: [] } as never);
  vi.spyOn(api, 'getCloudStatus').mockResolvedValue({ is_authenticated: false } as never);
  vi.spyOn(api, 'orcaCloudStatus').mockResolvedValue({ connected: false } as never);
  vi.spyOn(api, 'getFilamentPresetInfo').mockResolvedValue({ flow_ratio: null } as never);
  return vi
    .spyOn(api, 'bakeCalibrationForVerification')
    .mockResolvedValue({ blob: new Blob(['x']), filename: 'calibration_pa_tower.bake.3mf' });
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <CalibrationVerifyDownloadPage printerId={1} caliMode="pa_tower" onBack={() => {}} onDone={() => {}} />
      </ToastProvider>
    </QueryClientProvider>,
  );
}
const bakeButton = () => screen.getByRole('button', { name: 'Download pre-slice 3MF' });
const sliceButton = () => screen.getByRole('button', { name: 'Download sliced 3MF' });

beforeEach(() => {
  localStorage.clear();
  __resetSharedMemoryForTests();
  __resetPresetContextForTests();
  auth.permissions = new Set(['library:upload', 'printers:read', 'pipelines:read']);
  URL.createObjectURL = vi.fn(() => 'blob:calibration');
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => vi.restoreAllMocks());

describe('CalibrationVerifyDownloadPage', () => {
  it('the page renders the section in verify mode', async () => {
    expect(pageSource).toMatch(/<CalibrationSlicingSection[\s\S]*?mode="verify"/);
    expect(pageSource).not.toContain('function pickDefaultRef');
    expect(pageSource).not.toContain("from './preset-picker/");
    spies({ orca: false, bambu: false });
    mount();
    expect(await screen.findByLabelText('Printer profile')).toBeInTheDocument();
  });

  it('shows the family’s source segment, Mine at first, and no calibration copy of it', async () => {
    // Moved from SliceModalPickers (plan r6 Task 18): calibration's own
    // owner filter is gone; its pages show the family's source segment.
    spies({ orca: false, bambu: false });
    mount();
    const segment = await screen.findByRole('group', { name: 'Preset source' });
    expect(within(segment).getByRole('button', { name: 'My Presets' })).toHaveAttribute('aria-pressed', 'true');
    await userEvent.setup().click(within(segment).getByRole('button', { name: 'All' }));
    expect(within(segment).getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('bake is available while slicing is blocked, and sends the effective slicer', async () => {
    // Without the right to slice there is no catalogue: the sliced download is
    // blocked, the pre-slice bake keeps its own readiness.
    auth.permissions = new Set(['printers:read']);
    const bake = spies({ orca: true, bambu: false });
    mount();
    await screen.findByText(/needs the right to slice/i);
    expect(sliceButton()).toBeDisabled();
    await waitFor(() => expect(bakeButton()).toBeEnabled());
    await waitFor(async () => {
      await userEvent.setup().click(bakeButton());
      expect(bake).toHaveBeenCalled();
    });
    const body = bake.mock.calls.at(-1)?.[1] as CalibBakeOnlyIn;
    expect(body.slicer).toBe('orcaslicer');
  });

  it('bake with the slicer still unknown omits the field', async () => {
    const bake = spies('pending');
    mount();
    await waitFor(() => expect(bakeButton()).toBeEnabled());
    await userEvent.setup().click(bakeButton());
    await waitFor(() => expect(bake).toHaveBeenCalledTimes(1));
    const body = bake.mock.calls[0][1] as CalibBakeOnlyIn;
    expect('slicer' in body).toBe(false);
  });

  // Review I-2: a new authorisation context whose catalogue has not arrived
  // (or failed) holds the sliced download; the old context's refs are never
  // sent. The pre-slice bake keeps its own readiness.
  it('the sliced download waits for the new context’s catalogue, then sends its refs (review I-2)', async () => {
    const bake = spies({ orca: true, bambu: true });
    const slice = vi
      .spyOn(api, 'sliceCalibrationForVerification')
      .mockResolvedValue({ blob: new Blob(['x']), filename: 'probe.3mf' });
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(sliceButton()).toBeEnabled(), { timeout: 5000 });
    let release: (v: UnifiedPresetsResponse) => void = () => undefined;
    vi.mocked(api.getSlicerPresets).mockImplementation(
      () => new Promise<UnifiedPresetsResponse>((resolve) => (release = resolve)),
    );
    act(() => bumpPresetContext());
    await waitFor(() => expect(api.getSlicerPresets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(sliceButton()).toBeDisabled());
    await user.click(sliceButton());
    expect(slice).not.toHaveBeenCalled();
    await user.click(bakeButton());
    await waitFor(() => expect(bake).toHaveBeenCalledTimes(1));

    act(() => release(LISTING));
    await waitFor(() => expect(sliceButton()).toBeEnabled());
    await user.click(sliceButton());
    await waitFor(() => expect(slice).toHaveBeenCalledTimes(1));
    expect(slice.mock.calls[0][1].printer_preset).toEqual({ source: 'standard', id: 'Bambu Lab A1 0.4 nozzle' });
  });

  it('a failed catalogue of the new context keeps the sliced download blocked (review I-2)', async () => {
    spies({ orca: true, bambu: true });
    const slice = vi
      .spyOn(api, 'sliceCalibrationForVerification')
      .mockResolvedValue({ blob: new Blob(['x']), filename: 'probe.3mf' });
    mount();
    await waitFor(() => expect(sliceButton()).toBeEnabled());
    vi.mocked(api.getSlicerPresets).mockRejectedValue(new Error('down'));
    act(() => bumpPresetContext());
    await waitFor(() => expect(api.getSlicerPresets).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(sliceButton()).toBeDisabled());
    await userEvent.setup().click(sliceButton());
    expect(slice).not.toHaveBeenCalled();
  });

  // Review I-2 (second round): the section reports to the page one commit
  // later. On the FIRST commit of a new context the page must already refuse,
  // from the catalogue it reads itself — a click there sent the old refs.
  it('the first commit without the new catalogue neither enables nor sends (review I-2)', async () => {
    spies({ orca: true, bambu: true });
    const slice = vi
      .spyOn(api, 'sliceCalibrationForVerification')
      .mockResolvedValue({ blob: new Blob(['x']), filename: 'probe.3mf' });
    const commitsWithoutCatalogue: boolean[] = [];
    let armed = false;
    function ObserveCommit() {
      const catalogue = usePresetCatalogue();
      useLayoutEffect(() => {
        if (!armed || catalogue.data !== undefined) return;
        const button = sliceButton() as HTMLButtonElement;
        commitsWithoutCatalogue.push(button.disabled);
        if (commitsWithoutCatalogue.length === 1 && !button.disabled) button.click();
      });
      return <CalibrationVerifyDownloadPage printerId={1} caliMode="pa_tower" onBack={() => {}} onDone={() => {}} />;
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <ObserveCommit />
        </ToastProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(sliceButton()).toBeEnabled());
    vi.mocked(api.getSlicerPresets).mockImplementation(() => new Promise<UnifiedPresetsResponse>(() => {}));
    armed = true;
    act(() => bumpPresetContext());
    await waitFor(() => expect(sliceButton()).toBeDisabled());
    expect(commitsWithoutCatalogue).not.toHaveLength(0);
    expect({ firstDisabled: commitsWithoutCatalogue[0], sends: slice.mock.calls.length }).toEqual({ firstDisabled: true, sends: 0 });
  });

  // Review J-1: two different answers under one key can share dataUpdatedAt
  // (same millisecond). The revision must tell them apart.
  it('a different answer written in the same millisecond still withdraws the old decision (review J-1)', async () => {
    spies({ orca: true, bambu: true });
    const slice = vi
      .spyOn(api, 'sliceCalibrationForVerification')
      .mockResolvedValue({ blob: new Blob(['x']), filename: 'probe.3mf' });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let armed = false;
    const commits: boolean[] = [];
    function ObserveCommit() {
      const catalogue = usePresetCatalogue();
      useLayoutEffect(() => {
        if (!armed || catalogue.data?.standard.process.length !== 0) return;
        const button = sliceButton() as HTMLButtonElement;
        commits.push(button.disabled);
        if (commits.length === 1 && !button.disabled) button.click();
      });
      return <CalibrationVerifyDownloadPage printerId={1} caliMode="pa_tower" onBack={() => {}} onDone={() => {}} />;
    }
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <ObserveCommit />
        </ToastProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(sliceButton()).toBeEnabled());
    const query = client.getQueryCache().findAll({ queryKey: ['slicerPresets'] }).find((q) => q.state.data !== undefined);
    if (!query) throw new Error('catalogue query missing');
    const firstTime = query.state.dataUpdatedAt;
    const withoutProcess = { ...LISTING, standard: { ...LISTING.standard, process: [] } };
    armed = true;
    act(() => {
      const clock = vi.spyOn(Date, 'now').mockReturnValue(firstTime);
      try {
        client.setQueryData(query.queryKey, withoutProcess);
      } finally {
        clock.mockRestore();
      }
    });
    await waitFor(() => expect(sliceButton()).toBeDisabled());
    expect(query.state.dataUpdatedAt).toBe(firstTime);
    expect(commits.length).toBeGreaterThan(0);
    expect({ firstDisabled: commits[0], sends: slice.mock.calls.length }).toEqual({ firstDisabled: true, sends: 0 });
  });
});
