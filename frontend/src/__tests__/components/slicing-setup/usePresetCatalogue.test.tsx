import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, type CloudLoginResponse, type OrcaDevicePollResponse, type UnifiedPresetsResponse } from '../../../api/client';
import { usePresetCatalogue } from '../../../components/slicing-setup/usePresetCatalogue';
import { usePresetAuthKey } from '../../../components/slicing-setup/authContext';
import { __resetPresetContextForTests, presetContextGeneration } from '../../../utils/presetContext';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';

// Task 6 (plan r6, R10): one catalogue key family, carrying a monotonic
// authorisation generation; nothing of an old context is read in a new one.
const auth = { permissions: new Set<string>(['library:upload']), userId: 1 };
vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: auth.userId }, hasPermission: (p: string) => auth.permissions.has(p) }),
}));

const empty = { printer: [], process: [], filament: [] };
const catalogue = (tag: string): UnifiedPresetsResponse =>
  ({
    local: { ...empty, process: [{ id: tag, name: tag, source: 'local' }] },
    orca_cloud: empty,
    cloud: empty,
    standard: empty,
    cloud_status: 'ok',
    orca_cloud_status: 'ok',
  }) as unknown as UnifiedPresetsResponse;
const tagOf = (data: UnifiedPresetsResponse | undefined) => data?.local.process[0]?.id;

function Probe({ log, enabled }: { log: (string | undefined)[]; enabled?: boolean }) {
  const c = usePresetCatalogue({ enabled });
  log.push(tagOf(c.data));
  probe.refresh = c.refresh;
  probe.isError = c.isError;
  return null;
}
const probe: { refresh: (() => Promise<void>) | null; isError: boolean } = { refresh: null, isError: false };

function Key({ log }: { log: (string | null)[] }) {
  log.push(usePresetAuthKey());
  return null;
}

const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

beforeEach(() => {
  __resetPresetContextForTests();
  auth.permissions = new Set(['library:upload']);
  auth.userId = 1;
});
afterEach(() => vi.restoreAllMocks());

describe('usePresetAuthKey / usePresetCatalogue', () => {
  it('uses and caches Bambu detail for missing bases and compatibility overrides', async () => {
    const data = catalogue('thin');
    data.cloud = {
      printer: [],
      process: [
        { id: 'PPUS1', name: '0.20mm P1S chornobomb', source: 'cloud' },
        { id: 'GP001', name: '0.20mm Standard @BBL X1C', source: 'cloud' },
      ],
      filament: [{ id: 'PFUS1', name: 'My 0.6 mm PETG', source: 'cloud', inherits: 'Generic PETG @BBL A1M' }],
    };
    vi.spyOn(api, 'getSlicerPresets').mockResolvedValue(data);
    const detail = vi.spyOn(api, 'getCloudSettingDetail').mockResolvedValueOnce({
      name: '0.20mm P1S chornobomb', public: false, type: 'print', base_id: 'GP001',
      setting: { inherits: '0.20mm Standard @BBL X1C' },
    }).mockResolvedValueOnce({
      name: 'My 0.6 mm PETG', public: false, type: 'filament',
      setting: { compatible_printers: '"Bambu Lab A1 mini (0.6mm)";"Bambu Lab A1 mini 0.6 nozzle"' },
    });
    const client = newClient();
    render(<QueryClientProvider client={client}><Probe log={[]} /></QueryClientProvider>);
    const key = ['slicerPresets', `g${presetContextGeneration()}`];
    await waitFor(() => expect(client.getQueryData<UnifiedPresetsResponse>(key)?.cloud.process[0]).toMatchObject({
      inherits: '0.20mm Standard @BBL X1C', inherits_id: 'GP001',
    }));
    expect(client.getQueryData<UnifiedPresetsResponse>(key)?.cloud.filament[0]).toMatchObject({
      compatible_printers: ['Bambu Lab A1 mini (0.6mm)', 'Bambu Lab A1 mini 0.6 nozzle'],
    });
    await act(async () => client.invalidateQueries({ queryKey: key }));
    expect(detail.mock.calls).toEqual([['PPUS1'], ['PFUS1']]);
    expect(data.cloud.process[0].inherits).toBeUndefined();
  });

  it('rights A → B → A give three keys', async () => {
    const keys: (string | null)[] = [];
    const client = newClient();
    const view = render(
      <QueryClientProvider client={client}>
        <Key log={keys} />
      </QueryClientProvider>,
    );
    const settled = () => keys.filter((k): k is string => k !== null).at(-1);
    await waitFor(() => expect(settled()).toBeDefined());
    const first = settled();
    auth.permissions = new Set(['library:upload', 'printers:read']);
    view.rerender(
      <QueryClientProvider client={client}>
        <Key log={keys} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(settled()).not.toBe(first));
    const second = settled();
    auth.permissions = new Set(['library:upload']);
    view.rerender(
      <QueryClientProvider client={client}>
        <Key log={keys} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(settled()).not.toBe(second));
    expect(new Set([first, second, settled()]).size).toBe(3);
  });

  it('the first commit after refreshUser revokes a right reads no data of the old key', async () => {
    const spy = vi.spyOn(api, 'getSlicerPresets').mockResolvedValueOnce(catalogue('old'));
    const log: (string | undefined)[] = [];
    const client = newClient();
    const view = render(
      <QueryClientProvider client={client}>
        <Probe log={log} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(log.at(-1)).toBe('old'));
    let release: (v: UnifiedPresetsResponse) => void = () => {};
    spy.mockImplementationOnce(() => new Promise((r) => (release = r)));
    const before = log.length;
    auth.permissions = new Set(['library:upload', 'printers:read']);
    view.rerender(
      <QueryClientProvider client={client}>
        <Probe log={log} />
      </QueryClientProvider>,
    );
    expect(log[before]).toBeUndefined();
    expect(log.slice(before)).not.toContain('old');
    await act(async () => release(catalogue('new')));
    await waitFor(() => expect(log.at(-1)).toBe('new'));
  });

  it('a late refresh answer for an old generation is not written', async () => {
    const spy = vi.spyOn(api, 'getSlicerPresets').mockResolvedValueOnce(catalogue('a'));
    const log: (string | undefined)[] = [];
    const client = newClient();
    const view = render(
      <QueryClientProvider client={client}>
        <Probe log={log} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(log.at(-1)).toBe('a'));
    let release: (v: UnifiedPresetsResponse) => void = () => {};
    spy.mockImplementationOnce(() => new Promise((r) => (release = r)));
    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = probe.refresh?.() ?? Promise.resolve();
    });
    spy.mockResolvedValueOnce(catalogue('b'));
    auth.permissions = new Set(['library:upload', 'printers:read']);
    view.rerender(
      <QueryClientProvider client={client}>
        <Probe log={log} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(log.at(-1)).toBe('b'));
    await act(async () => {
      release(catalogue('stale'));
      await pending;
    });
    expect(log.at(-1)).toBe('b');
  });

  it('enabled false sends no request', async () => {
    const spy = vi.spyOn(api, 'getSlicerPresets').mockResolvedValue(catalogue('x'));
    render(
      <QueryClientProvider client={newClient()}>
        <Probe log={[]} enabled={false} />
      </QueryClientProvider>,
    );
    await new Promise((r) => setTimeout(r, 20));
    expect(spy).not.toHaveBeenCalled();
  });

  it('a generation change drops generation keys but leaves bare unmigrated keys', async () => {
    vi.spyOn(api, 'getSlicerPresets').mockResolvedValue(catalogue('x'));
    const client = newClient();
    client.setQueryData(['slicerPresets'], catalogue('bare'));
    client.setQueryData(['slicer-presets'], catalogue('dash'));
    client.setQueryData(['slicerLoadedFilaments', 'g999'], { printers: [], families: {} });
    render(
      <QueryClientProvider client={client}>
        <Probe log={[]} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(client.getQueryData(['slicerLoadedFilaments', 'g999'])).toBeUndefined());
    expect(tagOf(client.getQueryData(['slicerPresets']))).toBe('bare');
    expect(tagOf(client.getQueryData(['slicer-presets']))).toBe('dash');
  });

  it('a relink with equal flags bumps the generation; pending answers do not', async () => {
    const before = presetContextGeneration();
    server.use(
      http.post('/api/v1/cloud/login', () =>
        HttpResponse.json({ success: true, needs_verification: true, message: '' } satisfies CloudLoginResponse),
      ),
      http.post('/api/v1/orca-cloud/device/poll', () =>
        HttpResponse.json({ status: 'authorization_pending', connected: false, email: null, user_id: null } as unknown as OrcaDevicePollResponse),
      ),
      http.post('/api/v1/cloud/verify', () =>
        HttpResponse.json({ success: true, needs_verification: false, message: '' } satisfies CloudLoginResponse),
      ),
      http.post('/api/v1/orca-cloud/logout', () => HttpResponse.json({ success: true })),
    );
    await api.cloudLogin('a@b.c', 'pw');
    await api.orcaCloudDevicePoll();
    expect(presetContextGeneration()).toBe(before);
    await api.cloudVerify('a@b.c', '123456');
    expect(presetContextGeneration()).toBe(before + 1);
    await api.orcaCloudLogout();
    expect(presetContextGeneration()).toBe(before + 2);
  });
});

// Review I-5: a declined forced refresh is not left to escape its click
// handler — the ordinary query of the same, still active key reads again, and
// shows its own error state when that read fails too.
describe('a declined forced refresh', () => {
  it('falls back to the ordinary read, which recovers', async () => {
    const read = vi
      .spyOn(api, 'getSlicerPresets')
      .mockResolvedValueOnce(catalogue('cached'))
      .mockRejectedValueOnce(new Error('temporary outage'))
      .mockResolvedValueOnce(catalogue('recovered'));
    const log: (string | undefined)[] = [];
    render(
      <QueryClientProvider client={newClient()}>
        <Probe log={log} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(log.at(-1)).toBe('cached'));
    await expect(probe.refresh?.()).resolves.toBeUndefined();
    await waitFor(() => expect(log.at(-1)).toBe('recovered'));
    expect(read).toHaveBeenCalledTimes(3);
  });

  it('shows the error state when the ordinary read is refused too', async () => {
    vi.spyOn(api, 'getSlicerPresets')
      .mockResolvedValueOnce(catalogue('cached'))
      .mockRejectedValue(new Error('down'));
    const log: (string | undefined)[] = [];
    render(
      <QueryClientProvider client={newClient()}>
        <Probe log={log} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(log.at(-1)).toBe('cached'));
    await expect(probe.refresh?.()).resolves.toBeUndefined();
    await waitFor(() => expect(probe.isError).toBe(true));
  });
});
