/**
 * Tests for the FilamentMapping component's Filament Track Switch (FTS)
 * handling (upstream Bambuddy #1162).
 *
 * The FTS accessory routes any AMS slot to either extruder dynamically. When
 * present (printer status `fila_switch.installed === true`), the per-extruder
 * dropdown filter must be suppressed — otherwise the print modal's filament
 * dropdown is empty since the printer reports info bits 8-11 = 0xE
 * (uninitialized) for every AMS unit.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FilamentMapping } from '../../components/PrintModal/FilamentMapping';
import type { PrinterStatus } from '../../api/client';

const mockFilamentReqs = {
  filaments: [
    // Required filament asks for the LEFT extruder (nozzle_id=1). Pre-#1722 the
    // dropdown filtered to slots with extruderId=1; it now offers every slot.
    { slot_id: 1, type: 'PETG', color: '#00FF00', used_grams: 25, used_meters: 8.5, nozzle_id: 1 },
  ],
};

function createStatus(overrides: Partial<PrinterStatus>): PrinterStatus {
  return {
    id: 1,
    name: 'X2D',
    connected: true,
    state: 'IDLE',
    ams: [
      {
        id: 0,
        // Realistic FTS-installed bundle: AMS reports extruder bits 8-11 = 0xE,
        // so ams_extruder_map ends up empty.
        tray: [
          { id: 0, tray_type: 'PLA', tray_color: 'FF0000', tray_info_idx: 'GFA00', tray_sub_brands: 'Bambu PLA' },
          { id: 1, tray_type: 'PETG', tray_color: '00FF00', tray_info_idx: 'GFG00', tray_sub_brands: 'Bambu PETG' },
        ],
      },
    ],
    vt_tray: [],
    ams_extruder_map: {},
    ...overrides,
  } as PrinterStatus;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('FilamentMapping — FTS routing', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/:id/spool-assignments', () => HttpResponse.json([])),
    );
  });

  it('shows all loaded slots in the dropdown when FTS is installed', async () => {
    server.use(
      http.get(
        '/api/v1/printers/:id/status',
        () =>
          HttpResponse.json(
            createStatus({
              fila_switch: {
                installed: true,
                in_slots: [-1, 1],
                out_extruders: [0, 1],
                stat: 0,
                info: 2,
              },
            }),
          ),
      ),
    );

    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={mockFilamentReqs}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
      />,
    );

    // Both PLA and PETG slots must appear in the dropdown despite ams_extruder_map
    // being empty and the requirement asking for nozzle 1. Without the FTS guard
    // the dropdown would render only the "-- Select slot --" placeholder.
    await waitFor(() => {
      expect(screen.getByText(/Bambu PLA/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Bambu PETG/)).toBeInTheDocument();

    // The slot currently fed into a track gets an [L]/[R] badge. AMS-0 slot 1
    // (global tray ID 1) is in fila_switch.in_slots[1], whose track terminates
    // at extruder 1 → the LEFT-nozzle short label appears in that option.
    const petgOption = screen.getByText(/Bambu PETG/);
    expect(petgOption.textContent).toMatch(/\[L\]/);

    // AMS-0 slot 0 (global tray ID 0) is NOT currently fed into any track —
    // FTS routes it on demand, so no badge.
    const plaOption = screen.getByText(/Bambu PLA/);
    expect(plaOption.textContent).not.toMatch(/\[[LR]\]/);
  });

  it('renders the per-slot force-color-match checkbox when a handler is wired (#1717)', async () => {
    // When the caller passes onForceColorMatchChange the checkbox mounts and
    // bubbles toggle events up.
    server.use(
      http.get(
        '/api/v1/printers/:id/status',
        () =>
          HttpResponse.json(
            createStatus({
              fila_switch: null,
              ams_extruder_map: { '0': 1 },  // AMS 0 → left nozzle, matching the requirement
            }),
          ),
      ),
    );

    const onForceColorMatchChange = vi.fn();
    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={mockFilamentReqs}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
        forceColorMatch={{}}
        onForceColorMatchChange={onForceColorMatchChange}
      />,
    );

    const checkbox = await waitFor(() => {
      const cb = screen.getByLabelText(/Force color match/i) as HTMLInputElement;
      expect(cb).toBeInTheDocument();
      return cb;
    });
    expect(checkbox.checked).toBe(false);

    fireEvent.click(checkbox);
    expect(onForceColorMatchChange).toHaveBeenCalledTimes(1);
    expect(onForceColorMatchChange).toHaveBeenCalledWith(1, true);
  });

  it('omits the force-color-match checkbox when no handler is provided', async () => {
    // The checkbox is only meaningful when the caller is wired to persist the
    // toggle; absent a handler we must not render dead UI.
    server.use(
      http.get(
        '/api/v1/printers/:id/status',
        () =>
          HttpResponse.json(
            createStatus({
              fila_switch: null,
              ams_extruder_map: { '0': 1 },
            }),
          ),
      ),
    );

    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={mockFilamentReqs}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
      />,
    );

    // Wait for the panel to finish mounting (the Re-read button only renders once
    // printer status has loaded) before asserting the checkbox is absent.
    await waitFor(() => {
      expect(screen.getByText(/Re-read/i)).toBeInTheDocument();
    });
    expect(screen.queryByLabelText(/Force color match/i)).not.toBeInTheDocument();
  });

  it('offers cross-extruder slots when FTS is null (#1722)', async () => {
    server.use(
      http.get(
        '/api/v1/printers/:id/status',
        () =>
          HttpResponse.json(
            createStatus({
              fila_switch: null,
              ams_extruder_map: { '0': 0 },  // AMS 0 → right nozzle (extruder 0)
            }),
          ),
      ),
    );

    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={mockFilamentReqs}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
      />,
    );

    // Required nozzle is 1 (LEFT) but AMS 0 is on extruder 0 (RIGHT). #1722: the
    // dropdown no longer hides cross-extruder slots — the user may have loaded
    // the required filament into the "other" AMS on purpose, and the printer
    // firmware validates the ams_mapping at start-print. Both slots now appear.
    await waitFor(() => {
      expect(screen.getByText(/Bambu PLA/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Bambu PETG/)).toBeInTheDocument();
  });

  it('renders sub-brand + material-disambiguated colour on the required side (#1718)', async () => {
    // Required-side label was rendering the raw 3MF type ("PLA") and the
    // generic getColorName bucket ("Black").
    // After the shared useFilamentLabels hook it must now resolve
    // tray_info_idx → "Bambu PLA Matte" and the material-disambiguated
    // colour catalogue → "Charcoal" — the Specific-Printer panel matched
    // the Any-Model panel that was already correct.
    server.use(
      http.get(
        '/api/v1/printers/:id/status',
        () =>
          HttpResponse.json(
            createStatus({
              fila_switch: null,
              ams_extruder_map: { '0': 1 },
            }),
          ),
      ),
      http.get('/api/v1/cloud/builtin-filaments', () =>
        HttpResponse.json([{ filament_id: 'GFA01', name: 'Bambu PLA Matte' }]),
      ),
      http.get('/api/v1/cloud/filament-id-map', () => HttpResponse.json({})),
      http.get('/api/v1/inventory/colors/by-material', ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get('hex') === '#000000' && url.searchParams.get('material') === 'PLA Matte') {
          return HttpResponse.json({ color_name: 'Charcoal' });
        }
        return HttpResponse.json({ color_name: null });
      }),
    );

    const charcoalReqs = {
      filaments: [
        { slot_id: 1, type: 'PLA', color: '#000000', used_grams: 25, used_meters: 8.5, nozzle_id: 1, tray_info_idx: 'GFA01' },
      ],
    };

    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={charcoalReqs}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
      />,
    );

    // Required-side type text picks up the resolved sub-brand.
    await waitFor(() => {
      expect(screen.getByText(/Bambu PLA Matte/)).toBeInTheDocument();
    });
    // The swatch tooltip carries the disambiguated "Charcoal" instead of
    // the generic "Black" bucket; check the title attr on the colour
    // circle's parent span.
    await waitFor(() => {
      const swatch = screen.getByTitle(/Required: Bambu PLA Matte - Charcoal/);
      expect(swatch).toBeInTheDocument();
    });
  });
});
