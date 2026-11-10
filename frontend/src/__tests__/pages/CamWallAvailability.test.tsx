import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, ApiError } from '../../api/client';
import { CamWallPage } from '../../pages/CamWallPage';

vi.mock('../../components/CameraWall', () => ({ CameraWall: () => <div>Camera wall</div> }));

describe('G01 A CamWall credential failures', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.spyOn(api, 'getCamWallCameras').mockResolvedValue([]); });
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  it('shows a retryable outage for503, then recovers with the same kiosk token', async () => {
    const read = vi.spyOn(api, 'getCamWallPrinters').mockRejectedValue(new ApiError('unavailable', 503));
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0 } } });
    const { unmount } = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/camwall?token=shared-token']}><CamWallPage /></MemoryRouter></QueryClientProvider>);
    await act(() => vi.advanceTimersByTimeAsync(7500));
    expect(screen.getByText('Connection temporarily unavailable. Retrying automatically…')).toBeInTheDocument();
    expect(screen.queryByText(/expired|revoked/i)).toBeNull();
    const calls = read.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(500));
    expect(read).toHaveBeenCalledTimes(calls);
    read.mockResolvedValue([]);
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(screen.getByText('Camera wall')).toBeInTheDocument();
    for (const args of read.mock.calls) expect(args[0]).toBe('shared-token');
    unmount();
  });

  it('stops retrying after an explicit401 refusal', async () => {
    const read = vi.spyOn(api, 'getCamWallPrinters').mockRejectedValue(new ApiError('revoked', 401));
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: 0 } } });
    const { unmount } = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/camwall?token=revoked-token']}><CamWallPage /></MemoryRouter></QueryClientProvider>);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(read).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Camera wall')).toBeNull();
    unmount();
  });
});
