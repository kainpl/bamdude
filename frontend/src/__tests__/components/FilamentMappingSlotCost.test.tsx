/**
 * The print dialog prices each slot at the rate its print is charged and
 * shows the grams left on the spool bound to it (upstream 5649f836d).
 *
 * Both came from the built-in assignment rows in both inventory modes. In
 * Spoolman mode those hold nothing, so a print off a priced Spoolman spool was
 * estimated at the farm default — or "N/A" without one — and then archived at
 * the Spoolman price; the "g left" suffix never appeared.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, cleanup } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FilamentMapping } from '../../components/PrintModal/FilamentMapping';

const status = {
  id: 1,
  name: 'X1C',
  connected: true,
  state: 'IDLE',
  ams: [{ id: 0, tray: [{ id: 0, tray_type: 'PLA', tray_color: 'FF0000', tray_info_idx: 'GFA00' }] }],
  vt_tray: [],
  ams_extruder_map: {},
};

const spoolmanSpool = (over: Record<string, unknown>) => ({
  id: 42,
  material: 'PLA',
  subtype: 'Basic',
  brand: 'Ziro',
  color_name: 'Red',
  rgba: 'FF0000FF',
  label_weight: 250,
  weight_used: 62.6,
  cost_per_kg: null,
  effective_cost_per_kg: null,
  data_origin: 'spoolman',
  ...over,
});

// A built-in row that must never price a Spoolman print.
const builtInRow = [
  { id: 1, spool_id: 9, printer_id: 1, ams_id: 0, tray_id: 0, spool: { cost_per_kg: 99, label_weight: 1000, weight_used: 0 } },
];

function renderDialog(options: { spoolman: boolean; spool?: Record<string, unknown>; defaultCostPerKg?: number }) {
  server.use(
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/spoolman/status', () =>
      HttpResponse.json({ enabled: options.spoolman, connected: options.spoolman, url: null }),
    ),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json(builtInRow)),
    http.get('/api/v1/spoolman/inventory/slot-assignments/all', () =>
      HttpResponse.json([
        { printer_id: 1, printer_name: 'X1C', ams_id: 0, tray_id: 0, spoolman_spool_id: 42, ams_label: null },
        { printer_id: 2, printer_name: 'Other', ams_id: 0, tray_id: 0, spoolman_spool_id: 77, ams_label: null },
      ]),
    ),
    http.get('/api/v1/spoolman/inventory/spools', () =>
      HttpResponse.json([
        spoolmanSpool(options.spool ?? {}),
        spoolmanSpool({ id: 77, cost_per_kg: 500, effective_cost_per_kg: 500 }),
      ]),
    ),
  );
  render(
    <FilamentMapping
      printerId={1}
      filamentReqs={{ filaments: [{ slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 100, used_meters: 30 }] }}
      manualMappings={{}}
      onManualMappingChange={() => {}}
      currencySymbol="$"
      defaultCostPerKg={options.defaultCostPerKg ?? 25}
      defaultExpanded
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('FilamentMapping slot price and grams left', () => {
  it("estimates a Spoolman print at the bound spool's own rate", async () => {
    renderDialog({ spoolman: true, spool: { cost_per_kg: 48, effective_cost_per_kg: 48 } });
    await waitFor(() => expect(screen.getByText('$4.80')).toBeInTheDocument());
  });

  it("estimates a spool without a price of its own at its filament's", async () => {
    renderDialog({ spoolman: true, spool: { cost_per_kg: null, effective_cost_per_kg: 30 } });
    await waitFor(() => expect(screen.getByText('$3.00')).toBeInTheDocument());
  });

  it('estimates an unpriced Spoolman spool at the farm rate', async () => {
    renderDialog({ spoolman: true, spool: {} });
    await waitFor(() => expect(screen.getByText('$2.50')).toBeInTheDocument());
  });

  it('prices a Spoolman print even when the farm has no default rate', async () => {
    renderDialog({ spoolman: true, spool: { cost_per_kg: 48, effective_cost_per_kg: 48 }, defaultCostPerKg: 0 });
    await waitFor(() => expect(screen.getByText('$4.80')).toBeInTheDocument());
  });

  it("offers the slot with the bound Spoolman spool's grams left", async () => {
    renderDialog({ spoolman: true, spool: { cost_per_kg: 48, effective_cost_per_kg: 48 } });
    await waitFor(() => expect(screen.getByText('$4.80')).toBeInTheDocument());
    const options = screen.getAllByRole('combobox')[0].textContent;
    expect(options).toContain('187g left');
    expect(options).not.toContain('1000g left');
  });

  it('keeps pricing from the built-in assignment in built-in mode', async () => {
    renderDialog({ spoolman: false });
    await waitFor(() => expect(screen.getByText('$9.90')).toBeInTheDocument());
    expect(screen.getAllByRole('combobox')[0].textContent).toContain('1000g left');
  });
});
