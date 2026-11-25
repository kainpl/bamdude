/**
 * The Slice dialog on the slicing-setup family (plan r6 Task 16): one test per
 * allowed change of spec slicing-setup r5 §4.4, named after its item, plus the
 * first pick waiting for the dialog's own context and a pipeline that fills
 * slots by index.
 */
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router';
import { http, HttpResponse } from 'msw';

import '../utils';
import { server } from '../mocks/server';
import { SliceModal } from '../../components/SliceModal';
import { AuthProvider } from '../../contexts/AuthContext';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { __resetSharedMemoryForTests } from '../../components/slicing-setup/memory';
import type { LoadedFilamentsResponse } from '../../api/client';

const H2D04 = 'Bambu Lab H2D 0.4 nozzle';
const H2D06 = 'Bambu Lab H2D 0.6 nozzle';
const A104 = 'Bambu Lab A1 0.4 nozzle';
const CARD = 'Bambu Lab A1';
const H2D_PROCESS = '0.20mm Standard @BBL H2D';
const A1_PROCESS = '0.20mm Standard @BBL A1';
const BASIC = 'Bambu PLA Basic @BBL H2D';
const GENERIC = 'Generic PLA @BBL H2D';

const std = (name: string, extra: object = {}) => ({ id: name, name, source: 'standard', ...extra });
const none = { printer: [], process: [], filament: [] };
type Listing = {
  local: typeof none;
  orca_cloud: typeof none;
  cloud: typeof none;
  standard: typeof none;
  cloud_status: string;
  orca_cloud_status: string;
};
function listing(over: { printer?: string[]; process?: string[]; local?: Partial<typeof none> } = {}): Listing {
  return {
    local: { ...none, ...(over.local ?? {}) } as typeof none,
    orca_cloud: none,
    cloud: none,
    standard: {
      printer: (over.printer ?? [CARD, H2D06, H2D04, A104]).map((n) => std(n)),
      process: (over.process ?? [H2D_PROCESS, A1_PROCESS]).map((n) => std(n)),
      filament: [std(BASIC, { filament_type: 'PLA' }), std(GENERIC, { filament_type: 'PLA' })],
    } as unknown as typeof none,
    cloud_status: 'ok',
    orca_cloud_status: 'ok',
  };
}
const MY_PROCESS = { id: '7', name: 'My process', source: 'local' };
const FARM: LoadedFilamentsResponse = {
  printers: [],
  families: {},
  farm_printers: [{ printer_id: 1, model: 'H2D', accepts: [], nozzle_diameters: [0.4] }],
};

let presets: Listing = listing();
let farm: LoadedFilamentsResponse = FARM;

function serve(opts: { slots?: number; embedded?: string; health?: { orca: boolean; bambu: boolean }; pipelines?: object[] } = {}) {
  const health = opts.health ?? { orca: false, bambu: false };
  server.use(
    http.get('*/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }], ...(opts.embedded ? { embedded_printer: opts.embedded } : {}) }),
    ),
    http.get('*/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({
        file_id: 1,
        filename: 'Cube.3mf',
        filaments: Array.from({ length: opts.slots ?? 1 }, (_, i) => ({
          slot_id: i + 1,
          type: 'PLA',
          color: '#FFFFFF',
          used_grams: 1,
          used_meters: 1,
        })),
      }),
    ),
    http.get('*/api/v1/slicer/presets', () => HttpResponse.json(presets)),
    http.get('*/api/v1/slicer/printer-models', () => HttpResponse.json({ 'Bambu Lab H2D': 'H2D', 'Bambu Lab A1': 'A1' })),
    http.get('*/api/v1/slicer/loaded-filaments', () => HttpResponse.json(farm)),
    http.get('*/api/v1/slicer/health/:kind', ({ params }) => {
      const healthy = params.kind === 'orcaslicer' ? health.orca : health.bambu;
      return HttpResponse.json({ healthy, version: '2.3', error: healthy ? null : 'offline', url: null });
    }),
    http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
    http.get('*/api/v1/slicer-pipelines/', () => HttpResponse.json({ pipelines: opts.pipelines ?? [] })),
  );
}

function providers() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
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
  return Providers;
}

function open(id = 1, wrapper = providers()) {
  return render(<SliceModal source={{ kind: 'libraryFile', id, filename: 'Cube.3mf' }} onClose={() => undefined} />, { wrapper });
}
const field = (label: string) => waitFor(() => screen.getByLabelText(label)) as Promise<HTMLSelectElement>;
const valueOf = async (label: string, expected: string) => {
  const el = await field(label);
  await waitFor(() => expect(el.value).toBe(expected));
  return el;
};
const sliceButton = () => screen.getByRole('button', { name: 'Slice' });

beforeEach(() => {
  localStorage.clear();
  __resetSharedMemoryForTests();
  presets = listing();
  farm = FARM;
});
afterEach(() => localStorage.clear());

describe('Slice dialog on the slicing-setup family (§4.4)', () => {
  it('every picker follows the slicer and hides the other cloud', async () => {
    const tier = (source: string, tag: string) => ({
      printer: [std(H2D04, { id: `${tag}1`, source })],
      process: [std(H2D_PROCESS, { id: `${tag}2`, source }), std('0.28mm Draft @BBL H2D', { id: `${tag}4`, source })],
      filament: [std(BASIC, { id: `${tag}3`, source, filament_type: 'PLA' })],
    }) as unknown as typeof none;
    presets = { ...listing(), cloud: tier('cloud', 'stock'), orca_cloud: tier('orca_cloud', 'orca') };
    localStorage.setItem('bamdude:presets:source-filter', 'all');
    serve({ health: { orca: true, bambu: true } });
    server.use(http.get('*/api/v1/settings/ui-preferences', () => HttpResponse.json({ preferred_slicer: 'bambu_studio' })));
    open();
    const printer = await valueOf('Printer profile', 'cloud:stock1');
    const process = await field('Process profile');
    const filament = await field('Filament profile');
    const values = (select: HTMLSelectElement) => Array.from(select.options, (option) => option.value);
    expect(values(printer)).toContain('cloud:stock1');
    expect(values(process)).toContain('cloud:stock2');
    expect(values(filament)).toContain('cloud:stock3');
    expect([printer, process, filament].flatMap(values).join(' ')).not.toContain('orca_cloud:');
    fireEvent.change(process, { target: { value: 'cloud:stock4' } });
    expect(process).toHaveValue('cloud:stock4');
    fireEvent.click(screen.getByRole('button', { name: /OrcaSlicer/ }));
    await valueOf('Printer profile', 'orca_cloud:orca1');
    expect(values(process)).toContain('orca_cloud:orca2');
    expect(values(filament)).toContain('orca_cloud:orca3');
    expect([printer, process, filament].flatMap(values).some((value) => value.startsWith('cloud:'))).toBe(false);
    expect(sliceButton()).toBeDisabled();
    expect(screen.getByText(/selected profile is from the other slicer/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /BambuStudio/ }));
    await valueOf('Process profile', 'cloud:stock4');
  });
  it('§4.4.1 the source filter appears; the first time it is Mine', async () => {
    serve();
    open();
    const source = await screen.findByRole('group', { name: 'Preset source' });
    expect(within(source).getByRole('button', { name: 'My Presets' })).toHaveAttribute('aria-pressed', 'true');
    // No preset of your own: every field still offers something, and says why.
    expect((await screen.findAllByText(/Showing profiles from other sources/)).length).toBeGreaterThan(0);
  });

  it('§4.4.2 "Only printers that are online" becomes "Only models of my farm"', async () => {
    // With the default Mine and no printer of your own, the field relaxes
    // both filters (§4.4.1, §4.4.3): the farm narrowing is seen with All.
    localStorage.setItem('bamdude:presets:source-filter', 'all');
    serve();
    open();
    const toggle = await screen.findByRole('checkbox', { name: 'Only models of my farm' });
    expect(screen.queryByRole('checkbox', { name: 'Only printers that are online' })).toBeNull();
    await userEvent.setup().click(toggle);
    const printer = await field('Printer profile');
    await waitFor(() =>
      expect(Array.from(printer.options).map((o) => o.value).filter(Boolean)).toEqual([`standard:${H2D04}`]),
    );
  });

  it('§4.4.3 a filter that hides every fitting preset relaxes the field with a note', async () => {
    localStorage.setItem('bamdude:presets:source-filter', 'all');
    farm = { ...FARM, printers: [{ printer_id: 1, model: 'H2D', accepts: [], nozzle_diameters: [0.4], filaments: [{ kind: 'ams', material: 'PETG', family_id: 'GFG99', by_material: false }] }], families: {} };
    serve();
    open();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('checkbox', { name: 'Only filaments that are loaded' }));
    const note = await screen.findByText(/No profile of a loaded filament fits here/);
    await user.click(within(note).getByRole('button', { name: 'Show all' }));
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Only filaments that are loaded' })).not.toBeChecked());
  });

  it('§4.4.4 the printer fallback picks a 0.4 machine preset from the whole catalogue, never a model card', async () => {
    serve();
    open();
    await valueOf('Printer profile', `standard:${H2D04}`);
  });

  it('§4.4.5 a gone printer blocks Slice until a person picks, even when it comes back', async () => {
    serve();
    open();
    const user = userEvent.setup();
    const printer = await valueOf('Printer profile', `standard:${H2D04}`);
    await user.selectOptions(printer, `standard:${A104}`);
    await waitFor(() => expect(sliceButton()).toBeEnabled());

    presets = listing({ printer: [CARD, H2D06, H2D04] });
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(/no longer in the list/);
    expect(sliceButton()).toBeDisabled();

    presets = listing();
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(sliceButton()).toBeDisabled();

    await user.selectOptions(await field('Printer profile'), `standard:${A104}`);
    await waitFor(() => expect(sliceButton()).toBeEnabled());
  });

  it('§4.4.5 an open save row sends nothing for a gone printer, also when it comes back (review I-1)', async () => {
    let posts = 0;
    serve();
    server.use(
      http.post('*/api/v1/slicer-pipelines/', () => {
        posts += 1;
        return HttpResponse.json({ id: 9, name: 'x' });
      }),
    );
    open();
    const user = userEvent.setup();
    const printer = await valueOf('Printer profile', `standard:${H2D04}`);
    await user.selectOptions(printer, `standard:${A104}`);
    await waitFor(() => expect(sliceButton()).toBeEnabled());
    await user.click(screen.getByRole('button', { name: 'Save these settings' }));
    await user.type(screen.getByRole('textbox', { name: /name/i }), 'Mine');

    presets = listing({ printer: [CARD, H2D06, H2D04] });
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(/no longer in the list/);
    const save = screen.getByRole('button', { name: 'Save' });
    expect(save).toBeDisabled();
    fireEvent.click(save);

    presets = listing();
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await new Promise((r) => setTimeout(r, 50));
    expect(posts).toBe(0);
  });

  it('§4.4.6 the remembered slicer stores only a person’s choice', async () => {
    serve({ health: { orca: true, bambu: false } });
    const first = open();
    await waitFor(() => expect(screen.getByRole('button', { name: /OrcaSlicer/ })).toHaveAttribute('aria-pressed', 'true'));
    expect(localStorage.getItem('bamdude:slicer-pick:libraryFile:1')).toBeNull();
    first.unmount();

    serve({ health: { orca: true, bambu: true } });
    open();
    const bambu = await screen.findByRole('button', { name: /BambuStudio/ });
    await waitFor(() => expect(bambu).toBeEnabled());
    await userEvent.setup().click(bambu);
    expect(localStorage.getItem('bamdude:slicer-pick:libraryFile:1')).toBe('bambu_studio');
  });

  it('§4.4.7 changing the source re-picks the automatic process; the farm-models switch re-picks nothing', async () => {
    presets = listing({ local: { process: [MY_PROCESS] } as Partial<typeof none> });
    serve();
    open();
    const user = userEvent.setup();
    await valueOf('Process profile', 'local:7');
    await user.click(within(await screen.findByRole('group', { name: 'Preset source' })).getByRole('button', { name: 'All' }));
    await valueOf('Process profile', `standard:${H2D_PROCESS}`);
    await user.click(await screen.findByRole('checkbox', { name: 'Only models of my farm' }));
    await new Promise((r) => setTimeout(r, 50));
    expect((await field('Process profile')).value).toBe(`standard:${H2D_PROCESS}`);
    expect((await field('Printer profile')).value).toBe(`standard:${H2D04}`);
  });

  it('§4.4.8 a chosen preset among other printers keeps its note, also after the whole-list fallback', async () => {
    presets = listing({ process: [A1_PROCESS] });
    serve();
    open();
    await valueOf('Printer profile', `standard:${H2D04}`);
    const process = await field('Process profile');
    await userEvent.setup().selectOptions(process, `standard:${A1_PROCESS}`);
    expect(await screen.findByText(/No profile is known to fit this printer/)).toBeInTheDocument();
    expect(process).toHaveAccessibleDescription(/another printer/i);
  });

  it('warm catalogue before plates load: no first pick until the context is in', async () => {
    serve();
    const wrapper = providers();
    const first = open(1, wrapper);
    await valueOf('Printer profile', `standard:${H2D04}`);
    first.unmount();

    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.get('*/api/v1/library/files/2/plates', async () => {
        await gate;
        return HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }], embedded_printer: A104 });
      }),
    );
    open(2, wrapper);
    await new Promise((r) => setTimeout(r, 50));
    release();
    await valueOf('Printer profile', `standard:${A104}`);
  });

  it('a pipeline applies by slot index and keeps the tail', async () => {
    const pipeline = {
      id: 9,
      name: 'One filament',
      description: null,
      printer_preset: { source: 'standard', id: H2D04 },
      process_preset: { source: 'standard', id: H2D_PROCESS },
      filament_presets: [{ source: 'standard', id: GENERIC }],
      bed_type: null,
      created_by: null,
      created_at: '',
      updated_at: '',
    };
    serve({ slots: 2, pipelines: [pipeline] });
    open();
    await valueOf('Filament 1 (PLA)', `standard:${BASIC}`);
    await valueOf('Filament 2 (PLA)', `standard:${BASIC}`);
    await userEvent.setup().selectOptions(await field('Saved settings'), '9');
    await valueOf('Filament 1 (PLA)', `standard:${GENERIC}`);
    expect((await field('Filament 2 (PLA)')).value).toBe(`standard:${BASIC}`);
  });
});
