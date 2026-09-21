/**
 * The dialog has to route by the rule the printer routes by.
 *
 * Two ways it did not. With «Allow base-material match» ON the dialog swapped
 * the requirement's material for the family of its profile (PETG for a
 * PETG-CF file), so it offered spools the dispatcher would never accept — the
 * backend compares the material the FILE declares, canonicalised, and the
 * option only stops it asking about the profile id. With the option OFF it
 * asked for the same profile only when the family happened to be known, while
 * the backend compares the two ids wherever both are known: a job it refuses
 * as `variant_mismatch` was shown here as ready to print.
 *
 * Driven through the real dialog: the panel's own verdict is the assertion.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import type { PrintQueueItem } from '../../api/client';

const printers = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
];

const RED = 'FF0000';

/** One filament the sliced plate asks for. */
type Requirement = {
  type: string;
  tray_info_idx?: string;
  /** The family the profile resolves to — present only when we know it. */
  filament_type?: string;
};

/** One spool sitting in the AMS. */
type Tray = { tray_type: string; tray_info_idx: string };

/** A pending per-printer row whose stored routing already answered the option. */
const queueItem = (allowBaseMaterialMatch: boolean): PrintQueueItem => ({
  id: 7,
  queue_id: 1,
  printer_id: 1,
  archive_id: 1,
  library_file_id: null,
  waiting_reason: null,
  origin: 'queue',
  nozzle_offset_cali: 'off',
  mesh_mode_fast_check: true,
  execute_swap_macros: false,
  swap_macro_events: null,
  selected_macro_ids: null,
  gcode_injection: false,
  preheat_override: 'inherit',
  preheat_chamber_target_override: null,
  position: 1,
  scheduled_time: null,
  auto_off_after: false,
  manual_start: false,
  require_previous_success: false,
  ams_mapping: null,
  plate_id: null,
  bed_levelling: 'on',
  flow_cali: 'on',
  layer_inspect: false,
  timelapse: false,
  use_ams: true,
  status: 'pending',
  started_at: null,
  completed_at: null,
  error_message: null,
  created_at: '2024-01-01T00:00:00Z',
  archive_name: 'Bracket',
  archive_thumbnail: null,
  printer_name: 'X1 Carbon',
  print_time_seconds: 3600,
  filament_routing: {
    version: 1,
    mode: 'auto',
    feed_policy: 'ams_only',
    // Off, so the panel's headline reports the material verdict rather than
    // the colour one — the colours below agree anyway.
    force_color_match: false,
    allow_base_material_match: allowBaseMaterialMatch,
    filament_overrides: [],
  },
});

describe('what the print dialog counts as a match', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
    );
  });

  /** Open the dialog on a row that needs `requirement` against a printer loaded with `tray`. */
  const open = (allowBaseMaterialMatch: boolean, requirement: Requirement, tray: Tray) => {
    server.use(
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({
          connected: true,
          state: 'IDLE',
          ams: [{ id: 0, tray: [{ id: 0, tray_color: RED, ...tray }] }],
          vt_tray: [],
        }),
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () =>
        HttpResponse.json({
          filaments: [
            { slot_id: 1, color: `#${RED}`, used_grams: 10, used_meters: 3, ...requirement },
          ],
        }),
      ),
    );
    return render(
      <PrintModal
        mode="edit-queue-item"
        archiveId={1}
        archiveName="Bracket"
        queueItem={queueItem(allowBaseMaterialMatch)}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
  };

  it('matches the material the file declares, not the family its profile belongs to', async () => {
    // The option was never a licence to print PETG-CF on plain PETG: it drops
    // the profile-id question, it does not widen the material.
    open(
      true,
      { type: 'PETG-CF', tray_info_idx: 'Pa240002', filament_type: 'PETG' },
      { tray_type: 'PETG', tray_info_idx: 'GFG99' },
    );

    expect(await screen.findByText(/type not found/i)).toBeInTheDocument();
  });

  it('accepts the right material under another profile once the option is on', async () => {
    // Same material, different profile id — with the option on the id is
    // neither a condition nor a preference, so this is simply ready.
    open(
      true,
      { type: 'PETG-CF', tray_info_idx: 'Pa240002', filament_type: 'PETG' },
      { tray_type: 'PETG-CF', tray_info_idx: 'GFG99' },
    );

    expect(await screen.findByText(/ready/i)).toBeInTheDocument();
  });

  it('refuses another profile with the option off even when the family is unknown', async () => {
    // No `filament_type` here — the family never resolved. The backend still
    // compares the two ids it has and refuses; the dialog used to say «Ready».
    open(
      false,
      { type: 'PETG', tray_info_idx: 'Pa240002' },
      { tray_type: 'PETG', tray_info_idx: 'GFG99' },
    );

    expect(await screen.findByText(/filament variant does not match/i)).toBeInTheDocument();
    expect(screen.queryByText(/type not found/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/filament variant does not match/i).closest('button')!);
    expect(screen.getByTitle(/filament variant does not match/i)).toBeInTheDocument();
  });
});
