import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { AutoStockSpoolFields } from '../../components/AutoStockSpoolFields';

const permissions = vi.hoisted(() => ({ write: true }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ hasPermission: (p: string) => p === 'inventory:read' || permissions.write }) }));

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={queryClient}><AutoStockSpoolFields value={{ enabled: false, group: null }} onChange={vi.fn()} /></QueryClientProvider>);
}

describe('Automatic stock fields — fallback and permissions', () => {
  beforeEach(() => { permissions.write = true; });

  it('explains partial return preservation and the runout replacement contract', async () => {
    server.use(http.get('/api/v1/inventory/spools/auto-stock-groups', () => HttpResponse.json([])));
    mount();
    await userEvent.click(screen.getByText('How automatic assignment works'));
    const help = screen.getByText(/Removing and returning a known partial spool without runout/);
    expect(help).toBeVisible();
    expect(help).toHaveTextContent('including after restart');
    expect(help).toHaveTextContent('After confirmed runout, insertion means a new full spool');
    expect(help).toHaveTextContent('returning the old spool requires manual assignment');
    const externalHelp = screen.getByText(/External holders: after an unambiguous runout/);
    expect(externalHelp).toBeVisible();
    expect(externalHelp).toHaveTextContent('PAUSE → RUNNING');
    expect(externalHelp).toHaveTextContent('ordinary loading without runout still needs manual assignment');
    expect(externalHelp).toHaveTextContent('Both H2D external sides');
    expect(externalHelp).toHaveTextContent('never resumes the printer or changes its in-flight filament configuration');
  });

  it('shows a load failure rather than inventing a stock group', async () => {
    server.use(http.get('/api/v1/inventory/spools/auto-stock-groups', () => new HttpResponse(null, { status: 500 })));
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load inventory groups');
    expect(screen.getAllByRole('option')).toHaveLength(1);
  });

  it('keeps printer-only editors from enabling or changing a group', async () => {
    permissions.write = false;
    server.use(http.get('/api/v1/inventory/spools/auto-stock-groups', () => HttpResponse.json([])));
    mount();
    expect(await screen.findByLabelText('Assign a full stock spool on loading')).toBeDisabled();
    expect(screen.getByLabelText('Inventory filament group')).toBeDisabled();
    expect(screen.getByText('Inventory update permission is required to change automatic assignment.')).toBeVisible();
  });
});
