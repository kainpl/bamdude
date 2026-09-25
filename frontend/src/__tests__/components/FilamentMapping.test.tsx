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
              ams_switch_inlet: { '0': 'A' },
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

    // Each slot is badged for the switch INLET its AMS is plumbed into, using
    // the same L-for-In-A lettering as the printer card. Both slots are in
    // AMS 0, which is on In-A (upstream 7a42e0a7).
    expect(screen.getByText(/Bambu PETG/).textContent).toMatch(/\[L\]/);
    expect(screen.getByText(/Bambu PLA/).textContent).toMatch(/\[L\]/);
  });

  it('does not badge slots whose AMS has no inlet binding yet', async () => {
    // A switch fitted but not set up on the printer's Manual AMS Setup screen
    // reports no binding. Better a missing badge than a made-up one.
    server.use(
      http.get(
        '/api/v1/printers/:id/status',
        () =>
          HttpResponse.json(
            createStatus({
              fila_switch: { installed: true, in_slots: [-1, 1], out_extruders: [0, 1], stat: 0, info: 2 },
              ams_switch_inlet: {},
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

    await waitFor(() => {
      expect(screen.getByText(/Bambu PETG/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Bambu PETG/).textContent).not.toMatch(/\[[LR]\]/);
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

  it('shows a colour-only assignment as a refusal when exact colour is required', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(createStatus({ fila_switch: null, ams_extruder_map: { '0': 1 } }))),
    );
    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={{ filaments: [{ ...mockFilamentReqs.filaments[0], color: '#FF00FF', strict_color_match: true }] }}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
        requireExactColor
      />,
    );
    await waitFor(() => expect(screen.getAllByText(/Color mismatch/i)).toHaveLength(2));
    expect(screen.getByTitle(/Color mismatch/i)).toBeInTheDocument();
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('');
  });

  it('keeps a different profile family incompatible until base matching is enabled', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(createStatus({ fila_switch: null, ams_extruder_map: { '0': 1 } }))),
    );
    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={{ filaments: [{ ...mockFilamentReqs.filaments[0], tray_info_idx: 'P333PETG', filament_type: 'PETG', strict_profile_match: true }] }}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
      />,
    );
    await waitFor(() => expect(screen.getAllByText(/filament variant does not match/i)).toHaveLength(2));
    expect(screen.getByTitle(/filament variant does not match/i)).toBeInTheDocument();
    expect(screen.queryByText(/type not found/i)).not.toBeInTheDocument();
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

  it('pins the gram usage so a long name cannot clip it (#2669)', async () => {
    // Name and grams used to share one truncating span, so a long resolved name
    // pushed the "(25g)" off the end — partly on a wide screen, entirely in
    // mobile portrait. The grams answer "does the spool have enough left?", so
    // they are the last thing that should be dropped.
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(createStatus({}))),
      http.get('/api/v1/cloud/builtin-filaments', () =>
        HttpResponse.json([{ filament_id: 'GFA01', name: 'Polymaker PolyTerra PLA Matte Charcoal Black' }]),
      ),
      http.get('/api/v1/cloud/filament-id-map', () => HttpResponse.json({})),
      http.get('/api/v1/inventory/colors/by-material', () => HttpResponse.json({ color_name: null })),
    );

    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={{
          filaments: [
            { slot_id: 1, type: 'PLA', color: '#000000', used_grams: 25, used_meters: 8.5, nozzle_id: 1, tray_info_idx: 'GFA01' },
          ],
        }}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
      />,
    );

    const grams = await screen.findByText('(25g)');
    // The grams never truncate and never shrink away.
    expect(grams.className).toContain('shrink-0');
    expect(grams.className).not.toContain('truncate');

    // The name is what truncates instead, and carries its own tooltip — the
    // point of truncating is that you cannot read the rest.
    const name = await screen.findByText('Polymaker PolyTerra PLA Matte Charcoal Black');
    expect(name.className).toContain('truncate');
    expect(name).toHaveAttribute('title', 'Polymaker PolyTerra PLA Matte Charcoal Black');

    // Separate siblings, so the name shrinking cannot take the grams with it.
    expect(name).not.toBe(grams);
    expect(grams.parentElement).toBe(name.parentElement);
  });
});

describe('FilamentMapping — FTS same-inlet advisory', () => {
  // Bambu's own guidance: a change between two filaments on the SAME switch
  // inlet retracts the outgoing one all the way back to its AMS before the
  // incoming one can be fed up the shared tube; across the two inlets it only
  // retracts as far as the switch. Every filament of a job behind one inlet
  // means every change takes the slow path — worth a word, never a block.
  const twoFilamentReqs = {
    filaments: [
      { slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 20, used_meters: 7, nozzle_id: 0 },
      { slot_id: 2, type: 'PETG', color: '#00FF00', used_grams: 25, used_meters: 8.5, nozzle_id: 1 },
    ],
  };

  // Two AMS units, one filament matching in each, so the pick is unambiguous.
  const twoAmsStatus = (amsSwitchInlet: Record<string, 'A' | 'B'>, installed = true): Partial<PrinterStatus> => ({
    ams: [
      { id: 0, tray: [{ id: 0, tray_type: 'PLA', tray_color: 'FF0000', tray_info_idx: 'GFA00', tray_sub_brands: 'Bambu PLA' }] },
      { id: 1, tray: [{ id: 0, tray_type: 'PETG', tray_color: '00FF00', tray_info_idx: 'GFG00', tray_sub_brands: 'Bambu PETG' }] },
    ],
    fila_switch: installed ? { installed: true, in_slots: [-1, -1], out_extruders: [1, 0], stat: 0, info: 0 } : null,
    ams_switch_inlet: amsSwitchInlet,
  } as Partial<PrinterStatus>);

  const renderWith = (amsSwitchInlet: Record<string, 'A' | 'B'>, installed = true) => {
    server.use(
      http.get('/api/v1/printers/:id/spool-assignments', () => HttpResponse.json([])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(createStatus(twoAmsStatus(amsSwitchInlet, installed)))),
    );
    render(
      <FilamentMapping
        printerId={1}
        filamentReqs={twoFilamentReqs}
        manualMappings={{}}
        onManualMappingChange={() => {}}
        currencySymbol="$"
        defaultCostPerKg={0}
        defaultExpanded
      />,
    );
  };

  it('warns when every filament for the print is behind one inlet', async () => {
    renderWith({ '0': 'A', '1': 'A' });
    // Names the inlet, so the operator knows which spool to move.
    expect(await screen.findByText(/on Filament Track Switch IN-A\./)).toBeInTheDocument();
    expect(screen.getByText(/same inlet is slower/i)).toBeInTheDocument();
  });

  it('stays quiet when the filaments are split across both inlets', async () => {
    renderWith({ '0': 'A', '1': 'B' });
    await waitFor(() => {
      expect(screen.getAllByText(/Bambu PETG/).length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/same inlet is slower/i)).not.toBeInTheDocument();
  });

  it('stays quiet when the bindings are not known', async () => {
    renderWith({});
    await waitFor(() => {
      expect(screen.getAllByText(/Bambu PETG/).length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/same inlet is slower/i)).not.toBeInTheDocument();
  });

  it('stays quiet once the switch is gone, whatever binding was last seen', async () => {
    renderWith({ '0': 'A', '1': 'A' }, false);
    await waitFor(() => {
      expect(screen.getAllByText(/Bambu PETG/).length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/same inlet is slower/i)).not.toBeInTheDocument();
  });
});
