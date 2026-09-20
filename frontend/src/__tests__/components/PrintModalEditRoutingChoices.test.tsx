/**
 * Editing a queued row has to SAVE the routing choices the dialog is showing.
 *
 * The edit branch built its PATCH body by hand and left out the four routing
 * fields the add path has always sent. The backend merges only what arrives
 * (`exclude_unset=True`), so an omitted field silently kept the stored answer:
 * flipping «Allow base-material match» in the edit form changed the screen and
 * nothing else, and the row went on being routed by the answer it was created
 * with. Pinned here through the real dialog and the real request body.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import type { PrintQueueItem } from '../../api/client';

const printers = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
];

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
    force_color_match: true,
    allow_base_material_match: allowBaseMaterialMatch,
    filament_overrides: [],
  },
});

describe('editing a queued row', () => {
  /** The body of the PATCH the dialog sent, or undefined if it sent none. */
  let patched: Record<string, unknown> | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    patched = undefined;
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] }),
      ),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
      http.patch('/api/v1/queue/:id', async ({ request }) => {
        patched = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 7, status: 'pending' });
      }),
    );
  });

  const open = (item: PrintQueueItem) =>
    render(
      <PrintModal
        mode="edit-queue-item"
        archiveId={1}
        archiveName="Bracket"
        queueItem={item}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

  /** Found by its role and its label, never by a class — this is the control
   *  the operator sees, whatever markup it is drawn with. */
  const baseMaterialToggle = () =>
    screen.findByRole('checkbox', { name: /allow base-material match/i });

  it('saves the base-material option turned off, and the rest of the routing answers with it', async () => {
    const user = userEvent.setup();
    open(queueItem(true));

    const toggle = await baseMaterialToggle();
    expect(toggle).toBeChecked();
    await user.click(toggle);
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(patched).toBeDefined());
    // The operator's answer is what reaches the server. Omitting it is the bug:
    // the PATCH merges only what arrives, so the stored `true` would survive.
    expect(patched).toMatchObject({
      allow_base_material_match: false,
      // The other three travel with it — the routing answers are one set, and
      // half of them arriving is the same silent loss in a different field.
      feed_policy: 'ams_only',
      force_color_match: true,
      filament_overrides: [],
    });
  });

  it('saves the base-material option turned on', async () => {
    const user = userEvent.setup();
    open(queueItem(false));

    const toggle = await baseMaterialToggle();
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(patched).toBeDefined());
    expect(patched).toMatchObject({
      allow_base_material_match: true,
      feed_policy: 'ams_only',
      force_color_match: true,
      filament_overrides: [],
    });
  });

  it('leaves an untouched dialog sending the stored answer back unchanged', async () => {
    const user = userEvent.setup();
    open(queueItem(false));

    expect(await baseMaterialToggle()).not.toBeChecked();
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(patched).toBeDefined());
    expect(patched).toMatchObject({ allow_base_material_match: false });
  });
});
