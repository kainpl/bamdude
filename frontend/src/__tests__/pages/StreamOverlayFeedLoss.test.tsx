import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, ApiError, type OverlayStatus } from '../../api/client';
import { StreamOverlayPage } from '../../pages/StreamOverlayPage';

// The overlay is composited into a broadcast and nobody watches the page
// itself. Figures the page can no longer refresh must not stay on it looking
// live: "RUNNING 45 % · 150 / 300" from an hour ago is worse than nothing.

const printing: OverlayStatus = {
  id: 1, name: 'Kiosk', camera_rotation: 0, connected: true, state: 'RUNNING', progress: 45,
  current_print: 'Benchy.gcode.3mf', gcode_file: null, remaining_time: 82, layer_num: 150, total_layers: 300,
  stg_cur_name: null, temperatures: { nozzle: 220, nozzle_target: 220 }, time_format: '24h',
  camera: { active: true, stalled: false },
};

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

function mount(search: string) {
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/overlay/1${search}`]}>
        <Routes><Route path="/overlay/:printerId" element={<StreamOverlayPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const figures = () => ({
  progress: screen.queryByText('45%'),
  layers: screen.queryByText('150'),
  nozzle: screen.queryByText('220°C'),
});
const noFigures = { progress: null, layers: null, nozzle: null };

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', function MockWebSocket(this: Record<string, unknown>) {
    this.close = vi.fn();
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe.each([
  ['Classic', '?token=kiosk-token&show=printer,filename,status,progress,layers,eta,nozzle'],
  ['Version 2', '?token=kiosk-token&design=2&show=printer,filename,status,progress,layers,eta,nozzle'],
])('the stream overlay whose feed is lost — %s', (_design, search) => {
  it('takes its figures down when its token is refused, and says so in one line', async () => {
    const feed = vi.spyOn(api, 'getOverlayStatus').mockResolvedValue(printing);
    mount(search);
    await advance(100);
    expect(figures().progress).not.toBeNull();

    feed.mockRejectedValue(new ApiError('revoked', 401));
    await advance(10_000);

    expect(figures()).toEqual(noFigures);
    expect(screen.queryByText('Benchy')).toBeNull();
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    expect(screen.getByText('This overlay’s token is not accepted')).toBeInTheDocument();
    // …and stays that way: a refusal is not retried.
    const calls = feed.mock.calls.length;
    await advance(300_000);
    expect(feed).toHaveBeenCalledTimes(calls);
    expect(screen.getByText('This overlay’s token is not accepted')).toBeInTheDocument();
  });

  it.each([401, 403])('says the token is not accepted instead of loading for ever (%i from the start)', async (code) => {
    vi.spyOn(api, 'getOverlayStatus').mockRejectedValue(new ApiError('refused', code));
    mount(search);
    await advance(100);

    expect(screen.getByText('This overlay’s token is not accepted')).toBeInTheDocument();
    expect(screen.queryByText('Loading...')).toBeNull();
    expect(screen.queryByAltText('Camera stream')).toBeNull();
  });

  it('shows an offline printer once the feed has not answered for half a minute, and recovers', async () => {
    const feed = vi.spyOn(api, 'getOverlayStatus').mockResolvedValue(printing);
    mount(search);
    await advance(100);
    expect(figures().progress).not.toBeNull();

    feed.mockRejectedValue(new Error('network'));
    await advance(20_000);
    // A short outage changes nothing on the broadcast.
    expect(figures().progress).not.toBeNull();
    await advance(15_000);

    expect(figures()).toEqual(noFigures);
    expect(screen.getByText('Printer offline')).toBeInTheDocument();
    // The file name stays, as for a printer that went offline mid-print.
    expect(screen.getByText('Benchy')).toBeInTheDocument();

    feed.mockResolvedValue(printing);
    await advance(40_000);
    expect(figures().progress).not.toBeNull();
    expect(screen.queryByText('Printer offline')).toBeNull();
  });

  it('says there is no answer when the server was never reached, and draws once it is', async () => {
    const feed = vi.spyOn(api, 'getOverlayStatus').mockRejectedValue(new Error('network'));
    mount(search);
    await advance(40_000);

    expect(screen.getByText('No answer from the server')).toBeInTheDocument();
    expect(screen.queryByText('Loading...')).toBeNull();

    feed.mockResolvedValue(printing);
    await advance(40_000);
    expect(figures().progress).not.toBeNull();
    expect(screen.queryByText('No answer from the server')).toBeNull();
  });
});

describe('the stream overlay in a signed-in browser', () => {
  it('shows an offline printer once its status has not been read for half a minute', async () => {
    vi.spyOn(api, 'getPrinter').mockResolvedValue({ id: 1, name: 'X1', camera_rotation: 0 } as never);
    const status = vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({
      connected: true, state: 'RUNNING', progress: 45, current_print: 'Benchy.gcode.3mf', remaining_time: 82,
      layer_num: 150, total_layers: 300, temperatures: {},
    } as never);
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ time_format: '24h' } as never);
    vi.spyOn(api, 'getWebSocketToken').mockResolvedValue({ token: 'ws' } as never);
    vi.spyOn(api, 'getCameraSourceStatus').mockResolvedValue({ active: true, stalled: false } as never);
    mount('?camera=false');
    await advance(100);
    expect(screen.getByText('45%')).toBeInTheDocument();

    status.mockRejectedValue(new Error('network'));
    await advance(40_000);

    expect(screen.queryByText('45%')).toBeNull();
    expect(screen.getByText('Printer offline')).toBeInTheDocument();
  });
});
