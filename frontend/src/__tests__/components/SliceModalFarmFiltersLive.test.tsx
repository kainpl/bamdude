/**
 * The Slice dialog's farm filters while the dialog stays open: what they say
 * when the farm can no longer be read, and who is offered them at all.
 *
 * Rendered with a query client the test holds, so "the answer was read again"
 * is something the test can cause — in the app the WebSocket events do it
 * (`useWebSocket.test.ts` pins those).
 */
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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
import type { LoadedFilamentsResponse } from '../../api/client';

const H2D = 'Bambu Lab H2D 0.4 nozzle';
const A1 = 'Bambu Lab A1 0.4 nozzle';
const X1C = 'Bambu Lab X1 Carbon 0.4 nozzle';

const h2d = (filaments: LoadedFilamentsResponse['printers'][number]['filaments']) => ({
  printer_id: 1,
  model: 'H2D',
  accepts: [],
  nozzle_diameters: [0.4],
  filaments,
});
const FARM: LoadedFilamentsResponse = {
  farm_printers: [],
  printers: [h2d([{ kind: 'ams', material: 'PLA', family_id: 'GFA00', by_material: false }])],
  families: {
    GFA00: { name: 'Bambu PLA Basic', cloud: [], orca_cloud: [], local: [], standard: ['Bambu PLA Basic @BBL H2D'] },
  },
};

// What the farm route answers next; a test swaps it while the dialog is open.
let farm: LoadedFilamentsResponse | number = FARM;
let farmRequests = 0;

function serve() {
  farm = FARM;
  farmRequests = 0;
  const standard = {
    printer: [A1, H2D, X1C].map((name) => ({ id: name, name, source: 'standard' })),
    process: [{ id: 'p', name: '0.20mm Standard @BBL H2D', source: 'standard' }],
    filament: ['Bambu PLA Basic @BBL H2D', 'Generic PLA @BBL H2D'].map((name) => ({
      id: name,
      name,
      source: 'standard',
      filament_type: 'PLA',
    })),
  };
  const none = { printer: [], process: [], filament: [] };
  server.use(
    http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] })),
    http.get('*/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({
        file_id: 1,
        filename: 'Cube.3mf',
        filaments: [{ slot_id: 1, type: 'PLA', color: '#FFFFFF', used_grams: 1, used_meters: 1 }],
      }),
    ),
    http.get('*/api/v1/slicer/presets', () =>
      HttpResponse.json({
        local: none,
        orca_cloud: none,
        cloud: none,
        standard,
        cloud_status: 'ok',
        orca_cloud_status: 'ok',
      }),
    ),
    http.get('*/api/v1/slicer/printer-models', () =>
      HttpResponse.json({ 'Bambu Lab H2D': 'H2D', 'Bambu Lab A1': 'A1', 'Bambu Lab X1 Carbon': 'X1C' }),
    ),
    http.get('*/api/v1/slicer/loaded-filaments', () => {
      farmRequests += 1;
      return typeof farm === 'number'
        ? HttpResponse.json({ detail: 'refused' }, { status: farm })
        : HttpResponse.json(farm);
    }),
    http.get('*/api/v1/slicer/health/:kind', () => HttpResponse.json({ healthy: false, error: 'offline', url: null })),
    http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
    http.get('*/api/v1/slicer-pipelines/', () => HttpResponse.json({ pipelines: [] })),
  );
}

function open() {
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
  render(<SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Cube.3mf' }} onClose={() => undefined} />, {
    wrapper: Providers,
  });
  /** What a WebSocket event does in the app: the farm answer is read again. */
  const readFarmAgain = () => client.invalidateQueries({ queryKey: ['slicerLoadedFilaments'] });
  return { readFarmAgain };
}

async function picker(label: string): Promise<HTMLSelectElement> {
  return waitFor(() => screen.getByLabelText(label)) as Promise<HTMLSelectElement>;
}
const offered = (select: HTMLSelectElement) =>
  Array.from(select.querySelectorAll('option'))
    .filter((o) => o.value !== '')
    .map((o) => o.textContent);
const onlineToggle = () => screen.findByRole('checkbox', { name: 'Only printers that are online' });
const loadedToggle = () => screen.findByRole('checkbox', { name: 'Only filaments that are loaded' });

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe('SliceModal — the farm answer goes stale while the dialog is open', () => {
  it('keeps filtering on the last answer and says it may be out of date, with a way to read again', async () => {
    const user = userEvent.setup();
    serve();
    const { readFarmAgain } = open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(3));
    await user.selectOptions(printer, `standard:${H2D}`);
    await user.click(await onlineToggle());
    await waitFor(() => expect(offered(printer)).toEqual([H2D]));

    farm = 500;
    await readFarmAgain();

    // Still narrowed — so it must not say that every profile is shown.
    await screen.findByText(/could not be read again.*may be out of date/);
    expect(offered(printer)).toEqual([H2D]);
    expect(screen.queryByText(/every profile is shown/)).toBeNull();

    farm = FARM;
    const before = farmRequests;
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(farmRequests).toBe(before + 1));

    await waitFor(() => expect(screen.queryByText(/may be out of date/)).toBeNull());
    expect(offered(printer)).toEqual([H2D]);
  });

  it('follows a new answer: a printer that came online is offered', async () => {
    const user = userEvent.setup();
    serve();
    const { readFarmAgain } = open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(3));
    await user.selectOptions(printer, `standard:${H2D}`);
    await user.click(await onlineToggle());
    await waitFor(() => expect(offered(printer)).toEqual([H2D]));

    farm = {
      ...FARM,
      printers: [...FARM.printers, { printer_id: 2, model: 'A1', accepts: [], nozzle_diameters: [0.4], filaments: [] }],
    };
    await readFarmAgain();

    await waitFor(() => expect(offered(printer).sort()).toEqual([A1, H2D].sort()));
  });
});

describe('SliceModal — why "only loaded filaments" leaves nothing', () => {
  const emptied = async (chosenPrinter: string) => {
    const user = userEvent.setup();
    open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(3));
    await user.selectOptions(printer, `standard:${chosenPrinter}`);
    const filament = await picker('Filament profile');
    await waitFor(() => expect(filament.value).not.toBe(''));
    await user.selectOptions(filament, '');
    await user.click(await loadedToggle());
    await waitFor(() => expect(offered(filament)).toHaveLength(0));
  };

  it('says no printer of the chosen model is online when that is the reason', async () => {
    serve();
    // an A1 preset; the only online printer is an H2D, and it IS loaded
    await emptied(A1);

    expect(screen.getByText(/No printer of the chosen model is online/)).toBeInTheDocument();
    expect(screen.queryByText('Nothing is loaded on the printers that are online.')).toBeNull();
  });

  it('says nothing is loaded when printers of the model are online and empty', async () => {
    serve();
    farm = { printers: [h2d([])], families: {}, farm_printers: [] };
    await emptied(H2D);

    expect(screen.getByText(/Nothing is loaded on the online printers of the chosen model/)).toBeInTheDocument();
  });
});

describe('SliceModal — the farm filters and the right to see printers', () => {
  const ONLINE_KEY = 'bamdude:slice-modal:only-online-printers';
  const LOADED_KEY = 'bamdude:slice-modal:only-loaded-filaments';
  const signedInWith = (permissions: string[]) =>
    http.get('/api/v1/auth/me', () =>
      HttpResponse.json({
        id: 2,
        username: 'slicer',
        role: 'user',
        is_active: true,
        is_admin: false,
        groups: [],
        permissions,
        created_at: '2024-01-01T00:00:00Z',
      }),
    );

  it('offers neither switch to someone who may not read printers, and changes nothing stored', async () => {
    localStorage.setItem(ONLINE_KEY, '1');
    localStorage.setItem(LOADED_KEY, '1');
    serve();
    server.use(signedInWith(['library:read', 'library:upload']));
    open();

    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(3));
    const filament = await picker('Filament profile');
    await waitFor(() => expect(offered(filament)).toHaveLength(2));
    expect(screen.queryByRole('checkbox', { name: 'Only printers that are online' })).toBeNull();
    expect(screen.queryByRole('checkbox', { name: 'Only filaments that are loaded' })).toBeNull();
    // Not asked, not narrowed, and the switches come back as they were left.
    expect(farmRequests).toBe(0);
    expect(localStorage.getItem(ONLINE_KEY)).toBe('1');
    expect(localStorage.getItem(LOADED_KEY)).toBe('1');
  });

  it('offers both to someone who may', async () => {
    serve();
    server.use(signedInWith(['library:read', 'library:upload', 'printers:read']));
    open();

    expect(await onlineToggle()).toBeInTheDocument();
    expect(await loadedToggle()).toBeInTheDocument();
  });

  it('says the filters are unavailable when the server refuses the read, and narrows nothing', async () => {
    localStorage.setItem(ONLINE_KEY, '1');
    serve();
    farm = 403;
    open();

    const printer = await picker('Printer profile');
    await screen.findByText(/need the right to view printers/);
    expect(offered(printer)).toHaveLength(3);
    // Not a failure to try again, and not "could not be read".
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(screen.queryByText(/could not be read/)).toBeNull();
  });

  it('stops narrowing when a read that once succeeded is refused', async () => {
    const user = userEvent.setup();
    serve();
    const { readFarmAgain } = open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(3));
    await user.selectOptions(printer, `standard:${H2D}`);
    await user.click(await onlineToggle());
    await waitFor(() => expect(offered(printer)).toEqual([H2D]));

    farm = 403;
    await readFarmAgain();

    await screen.findByText(/need the right to view printers/);
    expect(offered(printer)).toHaveLength(3);
  });
});

describe('SliceModal — "only online printers" and two printer presets of one name', () => {
  it('judges each by the printer its own source says it was saved from', async () => {
    const user = userEvent.setup();
    serve();
    // An imported "Shop printer" saved from an X1C (offline), and a Bambu Cloud
    // one of the same name saved from the H2D that is online.
    server.use(
      http.get('*/api/v1/slicer/presets', () => {
        const none = { printer: [], process: [], filament: [] };
        return HttpResponse.json({
          local: { ...none, printer: [{ id: 'Shop printer', name: 'Shop printer', source: 'local', inherits: X1C }] },
          orca_cloud: none,
          cloud: { ...none, printer: [{ id: 'PM9', name: 'Shop printer', source: 'cloud', inherits: H2D }] },
          standard: {
            printer: [{ id: A1, name: A1, source: 'standard' }],
            process: [{ id: 'p', name: '0.20mm Standard', source: 'standard' }],
            filament: [{ id: 'f', name: 'Generic PLA', source: 'standard', filament_type: 'PLA' }],
          },
          cloud_status: 'ok',
          orca_cloud_status: 'ok',
        });
      }),
    );
    open();
    const printer = await picker('Printer profile');
    await waitFor(() => expect(offered(printer)).toHaveLength(3));
    await user.selectOptions(printer, `standard:${A1}`);

    await user.click(await onlineToggle());

    const values = () =>
      Array.from(printer.querySelectorAll('option'))
        .map((o) => o.value)
        .filter(Boolean)
        .sort();
    // The chosen A1 stays; of the two namesakes only the H2D's is online.
    await waitFor(() => expect(values()).toEqual(['cloud:PM9', `standard:${A1}`].sort()));
  });
});
