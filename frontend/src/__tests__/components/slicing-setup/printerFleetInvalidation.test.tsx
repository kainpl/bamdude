import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router';
import { http, HttpResponse } from 'msw';
import type { ReactNode } from 'react';
import { server } from '../../mocks/server';
import { api, type LoadedFilamentsResponse, type Printer } from '../../../api/client';
import { ToastProvider } from '../../../contexts/ToastContext';
import { ArchivedPrintersPanel } from '../../../components/settings/ArchivedPrintersPanel';
import { EditPrinterModal } from '../../../pages/PrintersPage';
import { usePresetFilters } from '../../../components/slicing-setup/usePresetFilters';
import { FARM_MODELS_KEY, __resetSharedMemoryForTests } from '../../../components/slicing-setup/memory';
import { __resetPresetContextForTests } from '../../../utils/presetContext';

// Task 12 (plan r6, spec slicing-setup §8.1): a printer change reaches a mounted
// farm read through the real mutation paths, every time, and an answer that
// was in flight before the change is never the one the filter keeps.
vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1 }, hasPermission: () => true, authEnabled: true, isAdmin: true }),
}));

const archived = { id: 5, name: 'Retired', model: 'X1C', archived: true, archived_at: null, is_active: true, tags: [] } as unknown as Printer;
const edited = { id: 77, name: 'Synthetic A', model: 'P1S', nozzle_count: 1, is_active: true, tags: [], tag_ids: [], serial_number: 'S77', ip_address: '192.0.2.77' } as unknown as Printer;
const farm = (ids: number[]): LoadedFilamentsResponse => ({
  printers: [],
  families: {},
  farm_printers: ids.map((printer_id) => ({ printer_id, model: 'X1C', accepts: [], nozzle_diameters: [] })),
});

function Probe() {
  const f = usePresetFilters({ farmModels: true });
  return <span data-testid="farm">{f.farm ? f.farm.farm_printers.map((p) => p.printer_id).join(',') : 'none'}</span>;
}

function Shell({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <ToastProvider>{children}</ToastProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  __resetSharedMemoryForTests();
  __resetPresetContextForTests();
  localStorage.setItem(FARM_MODELS_KEY, '1');
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([archived])),
    http.post('/api/v1/printers/5/unarchive', () => HttpResponse.json({ ...archived, archived: false })),
    http.get('/api/v1/macros/swap-profiles', () => HttpResponse.json([])),
    http.post('/api/v1/printers/diagnostic', () => HttpResponse.json({ checks: [] })),
    http.get('/api/v1/inventory/spools/auto-stock-groups', () => HttpResponse.json([])),
    http.patch('/api/v1/printers/77', async ({ request }) => HttpResponse.json({ ...edited, ...((await request.json()) as object) })),
  );
});
afterEach(() => vi.restoreAllMocks());

describe('invalidatePrinterFleet through the printer mutation paths', () => {
  it("two printer changes in a row refetch a mounted farm read twice, with ['printers'] inactive", async () => {
    const spy = vi.spyOn(api, 'getSlicerLoadedFilaments').mockResolvedValue(farm([1]));
    render(
      <Shell>
        <Probe />
        <ArchivedPrintersPanel />
        <EditPrinterModal printer={edited} onClose={() => {}} />
      </Shell>,
    );
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /unarchive/i }));
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(3));
  });

  it('a first farm read in flight is replaced', async () => {
    let releaseOld: (value: LoadedFilamentsResponse) => void = () => {};
    const spy = vi
      .spyOn(api, 'getSlicerLoadedFilaments')
      .mockImplementationOnce(() => new Promise((resolve) => (releaseOld = resolve)))
      .mockResolvedValueOnce(farm([1, 5]));
    render(
      <Shell>
        <Probe />
        <ArchivedPrintersPanel />
      </Shell>,
    );
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    await userEvent.setup().click(await screen.findByRole('button', { name: /unarchive/i }));
    await waitFor(() => expect(screen.getByTestId('farm')).toHaveTextContent('1,5'));
    releaseOld(farm([1]));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByTestId('farm')).toHaveTextContent('1,5');
    expect(spy).toHaveBeenCalledTimes(2);
  });
});
