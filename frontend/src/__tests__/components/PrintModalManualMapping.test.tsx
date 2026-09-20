/**
 * A mapping the dialog computed is not a hand-pinned job.
 *
 * Every payload this dialog sends carries `ams_mapping` — the routing shown on
 * screen, whether or not anybody touched a slot. The server read its presence as
 * a physical selection, so ordinary adds were stored as pinned and the first
 * tray swap held them for review. The answer now travels as its own field, and
 * these tests drive the real controls to pin it: who chose the trays is stated,
 * never inferred, and a pin already on a saved row survives an edit about
 * something else.
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

/** One PLA slot to fill, and two loaded trays so the operator has a choice. */
const filamentReqs = {
  filaments: [{ slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 12, used_meters: 4 }],
};

const printerStatus = {
  id: 1,
  name: 'X1 Carbon',
  connected: true,
  state: 'IDLE',
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: 'PLA', tray_color: 'FF0000', tray_info_idx: 'GFA00', tray_sub_brands: 'Bambu PLA' },
        { id: 1, tray_type: 'PLA', tray_color: '00FF00', tray_info_idx: 'GFA00', tray_sub_brands: 'Bambu PLA Matte' },
      ],
    },
  ],
  vt_tray: [],
  ams_extruder_map: {},
};

const queueItem = (mode: 'auto' | 'pinned'): PrintQueueItem => ({
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
  ams_mapping: [0],
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
    version: 3,
    mode,
    feed_policy: 'auto',
    force_color_match: false,
    allow_base_material_match: true,
    filament_overrides: [],
  },
});

describe('who chose the trays', () => {
  /** Bodies of the requests the dialog sent. */
  let posted: Record<string, unknown> | undefined;
  let patched: Record<string, unknown> | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    posted = undefined;
    patched = undefined;
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(printerStatus)),
      http.get('/api/v1/printers/:id/spool-assignments', () => HttpResponse.json([])),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json(filamentReqs)),
      http.post('/api/v1/queue/', async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 9, status: 'pending', created_item_ids: [9] });
      }),
      http.patch('/api/v1/queue/:id', async ({ request }) => {
        patched = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 7, status: 'pending' });
      }),
    );
  });

  const openAdd = () =>
    render(
      <PrintModal
        mode="add-to-queue"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={[1]}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

  const openEdit = (item: PrintQueueItem) =>
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

  /** The slot dropdown, named by the tooltip the operator sees on it. */
  const slotPicker = (state: 'Auto-matched' | 'Manually selected') => screen.findByTitle(state);

  it('an ordinary add says nobody picked the slots', async () => {
    const user = userEvent.setup();
    openAdd();

    // The dialog HAS worked a mapping out and will send it — that is the point:
    // the array alone must no longer be read as a physical selection.
    await slotPicker('Auto-matched');
    await user.click(screen.getByRole('button', { name: /add to queue/i }));

    await waitFor(() => expect(posted).toBeDefined());
    expect(posted).toMatchObject({ manual_mapping: false });
    expect(posted!.ams_mapping).toBeDefined();
  });

  it('changing a slot by hand says so', async () => {
    const user = userEvent.setup();
    openAdd();

    // The other loaded tray — a real pick through the control the operator uses.
    await user.selectOptions(await slotPicker('Auto-matched'), '1');
    await slotPicker('Manually selected');
    await user.click(screen.getByRole('button', { name: /add to queue/i }));

    await waitFor(() => expect(posted).toBeDefined());
    expect(posted).toMatchObject({ manual_mapping: true });
  });

  it('a schedule change on an auto-routed row leaves it auto', async () => {
    const user = userEvent.setup();
    openEdit(queueItem('auto'));

    await user.click(await screen.findByRole('button', { name: /queue only/i }));
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(patched).toBeDefined());
    expect(patched).toMatchObject({ manual_mapping: false, manual_start: true });
  });

  it('a hand-pinned row keeps its pin through an edit that never touched the slots', async () => {
    const user = userEvent.setup();
    openEdit(queueItem('pinned'));

    await user.click(await screen.findByRole('button', { name: /queue only/i }));
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(patched).toBeDefined());
    expect(patched).toMatchObject({ manual_mapping: true, manual_start: true });
  });
});
