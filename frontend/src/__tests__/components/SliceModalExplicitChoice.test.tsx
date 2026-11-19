/**
 * A preset the operator chose by hand is never replaced by an automatic pass.
 *
 * The dialog pre-picks process and filament presets, and re-picks them when the
 * listing, the printer or the slot list changes. Those passes may replace what
 * THEY picked; a choice made by hand (or by applying saved settings) stays for
 * as long as the listing still has it — when it does not fit the printer the
 * dialog says so beside the field, and when it is gone from the listing the
 * dialog falls back and says that too.
 */
import { describe, expect, it } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { SliceModal } from '../../components/SliceModal';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import type { PresetRef, UnifiedPreset, UnifiedPresetsBySlot } from '../../api/client';

type Tier = 'local' | 'orca_cloud' | 'cloud' | 'standard';
type Listing = Partial<Record<Tier, Partial<Record<keyof UnifiedPresetsBySlot, Partial<UnifiedPreset>[]>>>>;
type SavedSet = {
  id: number;
  name: string;
  printer_preset: PresetRef;
  process_preset: PresetRef;
  filament_presets: PresetRef[];
};

const H2D = 'Bambu Lab H2D 0.4 nozzle';
const A1 = 'Bambu Lab A1 0.4 nozzle';
const P1S = 'Bambu Lab P1S 0.4 nozzle';

// What the listing route answers; a test swaps it before pressing Refresh.
let listing: Listing = {};
let listingReads = 0;

function serve(first: Listing, { plates = 1, saved = [] as SavedSet[], slotsOnPlate2 = 1 } = {}) {
  listing = first;
  listingReads = 0;
  const tier = (source: Tier) => {
    const of = (slot: keyof UnifiedPresetsBySlot) =>
      (listing[source]?.[slot] ?? []).map((p) => ({ id: p.name, source, ...p }));
    return { printer: of('printer'), process: of('process'), filament: of('filament') };
  };
  server.use(
    http.get('*/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({
        is_multi_plate: plates > 1,
        plates: Array.from({ length: plates }, (_, i) => ({ index: i + 1, name: `Plate ${i + 1}`, objects: [] })),
      }),
    ),
    http.get('*/api/v1/library/files/:id/filament-requirements', ({ request }) => {
      const slots = new URL(request.url).searchParams.get('plate_id') === '2' ? slotsOnPlate2 : 1;
      return HttpResponse.json({
        file_id: 1,
        filename: 'Cube.3mf',
        filaments: Array.from({ length: slots }, (_, i) => ({
          slot_id: i + 1,
          type: 'PLA',
          color: '#FFFFFF',
          used_grams: 1,
          used_meters: 1,
        })),
      });
    }),
    http.get('*/api/v1/slicer/presets', () => {
      listingReads += 1;
      return HttpResponse.json({
        local: tier('local'),
        orca_cloud: tier('orca_cloud'),
        cloud: tier('cloud'),
        standard: tier('standard'),
        cloud_status: 'ok',
        orca_cloud_status: 'ok',
      });
    }),
    http.get('*/api/v1/slicer/printer-models', () =>
      HttpResponse.json({
        'Bambu Lab H2D': 'H2D',
        'Bambu Lab A1': 'A1',
        'Bambu Lab P1S': 'P1S',
        'Bambu Lab X1 Carbon': 'X1C',
      }),
    ),
    http.get('*/api/v1/slicer/health/:kind', () => HttpResponse.json({ healthy: false, error: 'offline', url: null })),
    http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
    http.get('*/api/v1/slicer-pipelines/', () =>
      HttpResponse.json({
        pipelines: saved.map((p) => ({
          ...p,
          description: null,
          bed_type: null,
          created_by: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        })),
      }),
    ),
  );
}

function open() {
  localStorage.setItem('bamdude:presets:only-farm', 'off');
  localStorage.setItem('bamdude:presets:only-mine', 'off');
  return render(
    <SliceJobTrackerProvider>
      <SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Cube.3mf' }} onClose={() => undefined} />
    </SliceJobTrackerProvider>,
  );
}

async function picker(label: string): Promise<HTMLSelectElement> {
  return waitFor(() => screen.getByLabelText(label)) as Promise<HTMLSelectElement>;
}

/** Press Refresh and wait until the listing has been read again and applied. */
async function refresh(user: ReturnType<typeof userEvent.setup>, next: Listing) {
  listing = next;
  const before = listingReads;
  const button = screen.getByRole('button', { name: 'Refresh' });
  await user.click(button);
  await waitFor(() => expect(listingReads).toBeGreaterThan(before));
  await waitFor(() => expect(button).toBeEnabled());
}

const std = (id: string): PresetRef => ({ source: 'standard', id });

describe('SliceModal — a process chosen by hand', () => {
  // A P1S whose process presets are all named for the X1C, from a sidecar that
  // reports no `compatible_printers`: the matcher calls every one a mismatch.
  const p1s: Listing = {
    standard: {
      printer: [{ name: P1S }],
      process: [
        { name: '0.08mm Extra Fine @BBL X1C' },
        { name: '0.20mm Standard @BBL X1C' },
        { name: '0.28mm Extra Draft @BBL X1C' },
      ],
      filament: [{ name: 'Generic PLA', filament_type: 'PLA' }],
    },
  };

  it('survives Refresh on a P1S whose process presets are named @BBL X1C', async () => {
    const user = userEvent.setup();
    serve(p1s);
    open();

    const process = await picker('Process profile');
    await waitFor(() => expect(process.value).not.toBe(''));
    await user.selectOptions(process, 'standard:0.28mm Extra Draft @BBL X1C');
    // A listing that changed — what a refetch after a preset was added returns.
    await refresh(user, {
      standard: { ...p1s.standard, process: [...(p1s.standard?.process ?? []), { name: '0.24mm Draft @BBL X1C' }] },
    });

    expect(process.value).toBe('standard:0.28mm Extra Draft @BBL X1C');
  });

  it('falls back, and says so, when the chosen preset is gone from the listing', async () => {
    const user = userEvent.setup();
    serve(p1s);
    open();

    const process = await picker('Process profile');
    await waitFor(() => expect(process.value).not.toBe(''));
    await user.selectOptions(process, 'standard:0.28mm Extra Draft @BBL X1C');
    await refresh(user, {
      standard: { ...p1s.standard, process: (p1s.standard?.process ?? []).slice(0, 2) },
    });

    await waitFor(() => expect(process.value).not.toBe('standard:0.28mm Extra Draft @BBL X1C'));
    expect(process.value).not.toBe('');
    expect(await screen.findByText(/no longer in the list.*Process profile/)).toBeInTheDocument();
  });

  const twoPrinters: Listing = {
    standard: {
      printer: [{ name: A1 }, { name: H2D }],
      process: [
        { name: '0.16mm Optimal @BBL A1', compatible_printers: [A1] },
        { name: '0.20mm Standard @BBL A1', compatible_printers: [A1] },
        { name: '0.20mm Standard @BBL H2D', compatible_printers: [H2D] },
      ],
      filament: [
        { name: 'Bambu PLA Basic @BBL A1', filament_type: 'PLA', compatible_printers: [A1] },
        { name: 'Bambu PLA Basic @BBL H2D', filament_type: 'PLA', compatible_printers: [H2D] },
      ],
    },
  };

  it('is kept and marked when the printer is changed by hand; automatic picks follow the printer', async () => {
    const user = userEvent.setup();
    serve(twoPrinters);
    open();

    const process = await picker('Process profile');
    const filament = await picker('Filament profile');
    await waitFor(() => expect(process.value).toBe('standard:0.20mm Standard @BBL A1'));
    await waitFor(() => expect(filament.value).toBe('standard:Bambu PLA Basic @BBL A1'));
    expect(process).not.toHaveAccessibleDescription();

    await user.selectOptions(process, 'standard:0.16mm Optimal @BBL A1');
    await user.selectOptions(await picker('Printer profile'), `standard:${H2D}`);

    // The filament nobody chose follows the printer…
    await waitFor(() => expect(filament.value).toBe('standard:Bambu PLA Basic @BBL H2D'));
    // …the process somebody chose stays, and the dialog says it does not fit.
    expect(process.value).toBe('standard:0.16mm Optimal @BBL A1');
    expect(process).toHaveAccessibleDescription(/another printer/);
    expect(filament).not.toHaveAccessibleDescription();
  });

  it('applied from saved settings that change the printer is kept, marked, and the note says what changed', async () => {
    const user = userEvent.setup();
    serve(twoPrinters, {
      saved: [
        {
          id: 7,
          name: 'A1 set on the H2D',
          printer_preset: std(H2D),
          process_preset: std('0.16mm Optimal @BBL A1'),
          filament_presets: [std('Bambu PLA Basic @BBL A1')],
        },
      ],
    });
    open();

    const process = await picker('Process profile');
    const filament = await picker('Filament profile');
    await waitFor(() => expect(filament.value).toBe('standard:Bambu PLA Basic @BBL A1'));
    const saved = await picker('Saved settings');
    await waitFor(() => expect(within(saved).getByRole('option', { name: 'A1 set on the H2D' })).toBeInTheDocument());
    await user.selectOptions(saved, 'A1 set on the H2D');

    const printer = await picker('Printer profile');
    await waitFor(() => expect(printer.value).toBe(`standard:${H2D}`));
    // The set's own process and filament, though the matcher calls both a
    // mismatch for the set's printer.
    await waitFor(() => expect(process).toHaveAccessibleDescription(/another printer/));
    expect(process.value).toBe('standard:0.16mm Optimal @BBL A1');
    expect(filament.value).toBe('standard:Bambu PLA Basic @BBL A1');
    expect(filament).toHaveAccessibleDescription(/another printer/);
    // The filament was already the set's; the note names only what changed.
    expect(
      screen.getByText('Applied "A1 set on the H2D". Changed: Printer profile, Process profile'),
    ).toBeInTheDocument();
  });
});

describe('SliceModal — a filament chosen by hand from "Other printers"', () => {
  const base: Listing = {
    standard: {
      printer: [{ name: H2D }],
      process: [{ name: '0.20mm Standard @BBL H2D', compatible_printers: [H2D] }],
      filament: [
        { name: 'Bambu PLA Basic @BBL H2D', filament_type: 'PLA', compatible_printers: [H2D] },
        { name: 'Bambu PLA Basic @BBL A1', filament_type: 'PLA', compatible_printers: [A1] },
      ],
    },
  };
  const A1_PLA = 'standard:Bambu PLA Basic @BBL A1';

  it('survives Refresh', async () => {
    const user = userEvent.setup();
    serve(base);
    open();

    const filament = await picker('Filament profile');
    await waitFor(() => expect(filament.value).toBe('standard:Bambu PLA Basic @BBL H2D'));
    await user.selectOptions(filament, A1_PLA);
    await refresh(user, {
      standard: {
        ...base.standard,
        filament: [...(base.standard?.filament ?? []), { name: 'Generic PLA @BBL H2D', filament_type: 'PLA' }],
      },
    });

    expect(filament.value).toBe(A1_PLA);
    expect(filament).toHaveAccessibleDescription(/another printer/);
  });

  it('survives "Slice all plates" and a plate switch', async () => {
    const user = userEvent.setup();
    serve(base, { plates: 2 });
    open();

    const first = await picker('Filament profile');
    await waitFor(() => expect(first.value).toBe('standard:Bambu PLA Basic @BBL H2D'));
    await user.selectOptions(first, A1_PLA);

    await user.click(screen.getByRole('checkbox', { name: 'Slice all plates' }));
    await waitFor(() => expect(screen.queryByTitle('Plate 2')).toBeNull());
    expect((await picker('Filament profile')).value).toBe(A1_PLA);

    await user.click(screen.getByRole('checkbox', { name: 'Slice all plates' }));
    // Browse to plate 2 in the strip, then commit it on its card.
    await user.click(await screen.findByTitle('Plate 2'));
    await user.click(await screen.findByRole('button', { name: /Plate 2/ }));
    await waitFor(() => expect(screen.getByText(/Cube\.3mf.*Plate 2/)).toBeInTheDocument());
    await waitFor(async () => expect((await picker('Filament profile')).value).toBe(A1_PLA));
  });

  it('is forgotten when the plate switch renumbers the slots', async () => {
    const user = userEvent.setup();
    serve(base, { plates: 2, slotsOnPlate2: 2 });
    open();

    const first = await picker('Filament profile');
    await waitFor(() => expect(first.value).toBe('standard:Bambu PLA Basic @BBL H2D'));
    await user.selectOptions(first, A1_PLA);
    await user.click(await screen.findByTitle('Plate 2'));
    await user.click(await screen.findByRole('button', { name: /Plate 2/ }));

    // Slot 1 of the new plate is not slot 1 of the old one: picked afresh.
    const slot1 = await picker('Filament 1 (PLA)');
    await waitFor(() => expect(slot1.value).toBe('standard:Bambu PLA Basic @BBL H2D'));
  });

  it('of another material than the plate asks for survives Refresh', async () => {
    const user = userEvent.setup();
    const withPetg: Listing = {
      standard: {
        ...base.standard,
        filament: [
          ...(base.standard?.filament ?? []),
          { name: 'Bambu PETG HF @BBL H2D', filament_type: 'PETG', compatible_printers: [H2D] },
        ],
      },
    };
    serve(withPetg);
    open();

    const filament = await picker('Filament profile');
    await waitFor(() => expect(filament.value).toBe('standard:Bambu PLA Basic @BBL H2D'));
    await user.selectOptions(filament, 'standard:Bambu PETG HF @BBL H2D');
    await refresh(user, {
      standard: {
        ...withPetg.standard,
        filament: [...(withPetg.standard?.filament ?? []), { name: 'Generic PLA @BBL H2D', filament_type: 'PLA' }],
      },
    });

    expect(filament.value).toBe('standard:Bambu PETG HF @BBL H2D');
  });
});
