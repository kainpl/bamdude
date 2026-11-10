import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, ApiError } from '../../api/client';
import { StreamOverlayPage } from '../../pages/StreamOverlayPage';

const overlay = {
  id: 1, name: 'Kiosk', camera_rotation: 0, connected: true, state: 'IDLE', progress: 0,
  current_print: null, gcode_file: null, remaining_time: null, layer_num: null, total_layers: null,
  stg_cur_name: null, temperatures: {}, time_format: 'system' as const,
};

describe('G01 overlay auth availability', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  function mount() {
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0 } } });
    return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/overlay/1?token=kiosk-token']}>
      <Routes><Route path="/overlay/:printerId" element={<StreamOverlayPage />} /></Routes>
    </MemoryRouter></QueryClientProvider>);
  }

  it('keeps the token and recovers after a transient outage', async () => {
    const read = vi.spyOn(api, 'getOverlayStatus').mockRejectedValue(new ApiError('unavailable', 503));
    const { unmount } = mount();
    await act(() => vi.advanceTimersByTimeAsync(7500));
    const calls = read.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(500));
    expect(read).toHaveBeenCalledTimes(calls);
    read.mockResolvedValue(overlay);
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(screen.getByText('Printer is idle')).toBeInTheDocument();
    for (const args of read.mock.calls) expect(args).toEqual([1, 'kiosk-token']);
    unmount();
  });

  it('stops polling and cancels a pending image retry on terminal refusal', async () => {
    const read = vi.spyOn(api, 'getOverlayStatus').mockResolvedValue(overlay);
    const { unmount } = mount();
    await act(() => vi.advanceTimersByTimeAsync(100));
    fireEvent.error(screen.getByAltText('Camera stream'));
    read.mockRejectedValue(new ApiError('revoked', 401));
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    const calls = read.mock.calls.length;
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(read).toHaveBeenCalledTimes(calls);
    unmount();
  });
});
