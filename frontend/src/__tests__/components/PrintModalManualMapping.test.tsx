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

const baseQueueItem = (): PrintQueueItem => ({
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
});

const queueItem = (mode: 'auto' | 'pinned'): PrintQueueItem => ({
  ...baseQueueItem(),
  filament_routing: {
    version: 3,
    mode,
    feed_policy: 'auto',
    force_color_match: false,
    allow_base_material_match: true,
    filament_overrides: [],
  },
});

/** A row from before the routing column: no stored answer, only the old mapping. */
const legacyQueueItem = (amsMapping: number[] | null): PrintQueueItem => ({
  ...baseQueueItem(),
  ams_mapping: amsMapping,
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

  it('pins only the printer whose displayed assignment the operator changes', async () => {
    const user = userEvent.setup();
    const posts: Record<string, unknown>[] = [];
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([
        printers[0], { ...printers[0], id: 2, name: 'Second printer' },
      ])),
      http.post('/api/v1/auto-queue/printer-routing-preview', async ({ request }) => {
        const body = await request.json() as { targets: { printer_id: number; plate_id: number; ams_mapping?: number[] }[] };
        return HttpResponse.json({ targets: body.targets.map(target => ({ ...target,
          status: 'compatible', mapping: target.ams_mapping ?? [1], reason: null })) });
      }),
      http.post('/api/v1/queue/', async ({ request }) => {
        posts.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ id: posts.length, status: 'pending', created_item_ids: [posts.length] });
      }),
    );
    render(<PrintModal mode="add-to-queue" archiveId={1} archiveName="Bracket" initialSelectedPrinterIds={[1, 2]}
      lockPrinterSelection onClose={vi.fn()} onSuccess={vi.fn()} />);
    const custom = await screen.findAllByRole('checkbox', { name: /custom mapping/i });
    await user.click(custom[0]);
    await user.click(custom[1]);
    const picks = await screen.findAllByTitle('Auto-matched');
    // Server assignment wins over the local tray-0 provisional preference.
    await waitFor(() => expect(picks[0]).toHaveValue('1'));
    await user.selectOptions(picks[0], '0');
    const submit = screen.getByRole('button', { name: /queue to 2 printers/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[0]).toMatchObject({ queue_id: 1, manual_mapping: true, ams_mapping: [0] });
    expect(posts[1]).toMatchObject({ queue_id: 2, manual_mapping: false, ams_mapping: [1] });
  });

  it('a hand-pinned row keeps its pin through an edit that never touched the slots', async () => {
    const user = userEvent.setup();
    openEdit(queueItem('pinned'));

    await user.click(await screen.findByRole('button', { name: /queue only/i }));
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(patched).toBeDefined());
    expect(patched).toMatchObject({ manual_mapping: true, manual_start: true });
    expect(patched).not.toHaveProperty('ams_mapping');
    expect(patched?.remap_filament).toBe(false);
  });

  /**
   * A row queued before the routing column existed stores no answer at all, and
   * the server reads that absence as physical intent — pinned, and held for
   * review when the row has no usable mapping to check. Saying «nobody picked
   * the slots» for such a row would reclassify it from an edit that never
   * mentioned them, and lose the review with it. A missing mapping and one that
   * pins nothing are the two shapes where nothing else rescues the answer: an
   * older row WITH real trays seeds them into the dialog and reads as manual
   * on its own.
   */
  it.each([
    ['no mapping at all', null],
    ['a mapping that pins nothing', [-1, -1]],
  ] as const)('a row from before the routing column keeps its pin — %s', async (_label, amsMapping) => {
    const user = userEvent.setup();
    openEdit(legacyQueueItem(amsMapping as number[] | null));

    await user.click(await screen.findByRole('button', { name: /queue only/i }));
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(patched).toBeDefined());
    expect(patched).toMatchObject({ manual_mapping: true, manual_start: true });
  });
});
