/**
 * The print button tells the truth (spec Д6 / Ф7).
 *
 * The dialog used to offer «Add to Queue» whatever was in the trays: the button
 * asked only whether the FILE had been read, never whether anything selected
 * could print it as loaded. A plate whose material is not on the farm went out
 * looking queued and sat there.
 *
 * ⚠️ The verdict is taken from the full assignment under the current policy —
 * the very mapping this dialog would send — and never from one word of channel
 * status. Two counter-examples are pinned below, because both were the first
 * design and both are wrong: `type_only` is ALSO the answer for a strict-colour
 * channel with no source at all (mapping `-1`), and `mismatch` is ALSO the
 * answer for a pure profile veto with the material sitting right there.
 *
 * Unknown is not an incompatibility, but it needs explicit consent to wait.
 * A missing model is separate again. Busy compatible printers still accept
 * normal queue work: routing and dispatch readiness remain different things.
 */

import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import type { FilamentRoutingSnapshot, PrintQueueItem, RoutingPreview } from '../../api/client';

const printers = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
];

/** One AMS tray, as the status endpoint reports it. */
type Tray = { tray_type: string; tray_color: string; tray_info_idx?: string };

const statusWith = (trays: Tray[], extra: Record<string, unknown> = {}) => ({
  connected: true,
  state: 'IDLE',
  ams: [{ id: 0, tray: trays.map((tray, id) => ({ id, remain: 90, ...tray })) }],
  vt_tray: [],
  ...extra,
});

/** The routing answers a caller can pin onto a fresh dialog. */
const routing = (over: Partial<FilamentRoutingSnapshot> = {}): FilamentRoutingSnapshot => ({
  version: 1,
  mode: 'auto',
  feed_policy: 'auto',
  force_color_match: false,
  allow_base_material_match: true,
  filament_overrides: [],
  ...over,
});

function backendRefusal(code = 'material_mismatch', message = 'No compatible filament source is available. Channel 1 needs ABS.') {
  server.use(http.post('/api/v1/auto-queue/printer-routing-preview', async ({ request }) => {
    const data = await request.json() as { targets: { printer_id: number; plate_id: number }[] };
    return HttpResponse.json({ targets: data.targets.map(target => ({ ...target, status: 'incompatible', mapping: null, reason: { code, message } })) });
  }));
}

/** A pending per-printer row, for the edit-mode case. */
const queueItem = (): PrintQueueItem => ({
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
  created_at: '2026-01-01T00:00:00Z',
  archive_name: 'Bracket',
  archive_thumbnail: null,
  printer_name: 'X1 Carbon',
  print_time_seconds: 3600,
  filament_routing: routing(),
});

/** One compatibility group of the routing preview. */
const group = (counts: Partial<RoutingPreview['plates'][number]['groups'][number]>) => ({
  key: 'X1C/1/present',
  model: 'X1C',
  nozzles: 1,
  ams: 'present' as const,
  total: 1,
  compatible: 0,
  unknown: 0,
  incompatible: 0,
  ready: 0,
  reasons: [],
  ...counts,
});

const previewWith = (
  groups: ReturnType<typeof group>[],
  advisoryUnavailable = false,
): RoutingPreview => ({
  advisory_unavailable: advisoryUnavailable,
  plates: [
    {
      requested_plate_id: 1,
      plate_id: 1,
      status: 'ok',
      reason: null,
      model: 'X1C',
      filaments: [{ slot_id: 1, type: 'ABS', color: '#FF0000', nozzle_id: null, used_grams: 10 }],
      groups,
    },
  ],
});

describe('the print button and what the trays actually hold', () => {
  let queuePosts: number;
  let reprints: number;
  let onClose: Mock<() => void>;
  let onSuccess: Mock<() => void>;
  let onAutoSubmitRefused: Mock<() => void>;

  beforeEach(() => {
    vi.clearAllMocks();
    queuePosts = 0;
    reprints = 0;
    onClose = vi.fn<() => void>();
    onSuccess = vi.fn<() => void>();
    onAutoSubmitRefused = vi.fn<() => void>();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/archives/:id', () => HttpResponse.json({ id: 1, sliced_for_model: null })),
      http.get('/api/v1/archives/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] }),
      ),
      http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
      http.post('/api/v1/queue/', () => {
        queuePosts += 1;
        return HttpResponse.json({ id: 11, status: 'pending', created_item_ids: [11] });
      }),
      http.post('/api/v1/archives/:id/reprint', () => {
        reprints += 1;
        return HttpResponse.json({ status: 'started', archive_id: 1 });
      }),
    );
  });

  /** What the plate asks for. */
  const needs = (filaments: Record<string, unknown>[]) =>
    http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments }));

  const oneAbsChannel = [{ slot_id: 1, type: 'ABS', color: '#FF0000', used_grams: 10 }];

  const openQueueDialog = (props: Record<string, unknown> = {}) =>
    render(
      <PrintModal
        mode="add-to-queue"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={[1]}
        onClose={onClose}
        onSuccess={onSuccess}
        {...props}
      />,
    );

  const submitButton = () => screen.findByRole('button', { name: /add to queue/i });

  it.each(['add-to-queue', 'reprint'] as const)('partial PETG profile refusal needs consent in %s and never starts a direct print', async (mode) => {
    const user = userEvent.setup();
    server.use(
      needs([{ slot_id: 1, type: 'PETG', color: '#000000', used_grams: 244.6, tray_info_idx: 'P8e36324' }]),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWith([
        { tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99' },
      ]))),
      http.post('/api/v1/auto-queue/printer-routing-preview', async ({ request }) => {
        const data = await request.json() as { targets: { printer_id: number; plate_id: number }[] };
        return HttpResponse.json({ targets: data.targets.map(target => ({ ...target, status: 'unknown', mapping: null,
          reason: { code: 'variant_mismatch', message: 'Channel 1 needs PETG (P8e36324); loaded: PETG (GFG99).' } })) });
      }),
    );
    openQueueDialog({ mode, initialRouting: routing({ allow_base_material_match: false }) });
    const notice = await screen.findByTestId('feasibility-notice');
    expect(notice).toHaveTextContent(/not fully verified/i);
    expect(notice).toHaveTextContent(/P8e36324/);
    expect(notice).toHaveTextContent(/GFG99/);
    await waitFor(() => expect(screen.getByTestId('feasibility-notice').closest('form')?.querySelector('button[type="submit"]')).toBeDisabled());
    expect(screen.queryByText(/required filament type not found/i)).not.toBeInTheDocument();
    fireEvent.submit(screen.getByTestId('feasibility-notice').closest('form')!);
    expect(queuePosts).toBe(0);
    expect(reprints).toBe(0);
    await user.click(screen.getByTestId('feasibility-override'));
    const confirmation = await screen.findByRole('dialog', { name: /queue it anyway/i });
    expect(queuePosts).toBe(0);
    await user.click(within(confirmation).getByRole('button', { name: /wait for compatibility/i }));
    await waitFor(() => expect(queuePosts).toBe(1));
    expect(reprints).toBe(0);
  });

  it('a failed per-printer preview cannot silently enable print and offers a retry', async () => {
    server.use(needs(oneAbsChannel),
      http.post('/api/v1/auto-queue/printer-routing-preview', () => new HttpResponse(null, { status: 503 })));
    openQueueDialog({ mode: 'reprint' });
    expect(await screen.findByTestId('feasibility-notice')).toHaveTextContent(/not fully verified/i);
    expect(screen.getByRole('button', { name: /^print$/i })).toBeDisabled();
    expect(within(screen.getByTestId('feasibility-notice')).getByRole('button', { name: /retry/i })).toBeEnabled();
    fireEvent.submit(screen.getByTestId('feasibility-notice').closest('form')!);
    expect(reprints).toBe(0);
  });

  it('a manually selected wrong material cannot clear the backend refusal', async () => {
    const user = userEvent.setup();
    let manualSeen = false;
    server.use(needs(oneAbsChannel),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }]))),
      http.post('/api/v1/auto-queue/printer-routing-preview', async ({ request }) => {
        const data = await request.json() as { targets: { printer_id: number; plate_id: number; manual_mapping: boolean }[] };
        manualSeen ||= data.targets.some(target => target.manual_mapping);
        return HttpResponse.json({ targets: data.targets.map(target => ({ ...target, status: 'incompatible', mapping: null,
          reason: { code: 'material_mismatch', message: 'No compatible filament source is available.' } })) });
      }));
    openQueueDialog();
    await screen.findByTestId('feasibility-notice');
    await user.selectOptions(await screen.findByTitle('Auto-matched'), '0');
    await waitFor(() => expect(manualSeen).toBe(true));
    expect(await submitButton()).toBeDisabled();
    expect(queuePosts).toBe(0);
  });

  it('a backend feed-policy refusal is not overridden by a populated local mapping', async () => {
    backendRefusal('feed_topology_mismatch', 'The feed topology does not match.');
    server.use(needs(oneAbsChannel), http.get('/api/v1/printers/:id/status', () =>
      HttpResponse.json(statusWith([{ tray_type: 'ABS', tray_color: 'FF0000FF' }]))));
    openQueueDialog({ initialRouting: routing({ feed_policy: 'external_only' }) });
    expect(await screen.findByTestId('feasibility-notice')).toHaveTextContent('feed topology');
    expect(await submitButton()).toBeDisabled();
  });

  it('sends a manually selected wrong-colour tray instead of hiding it as an absent pin', async () => {
    const user = userEvent.setup();
    let manualMapping: number[] | undefined;
    server.use(needs(oneAbsChannel),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWith([{ tray_type: 'ABS', tray_color: '00FF00FF' }]))),
      http.post('/api/v1/auto-queue/printer-routing-preview', async ({ request }) => {
        const data = await request.json() as { targets: { printer_id: number; plate_id: number; manual_mapping: boolean; ams_mapping?: number[] }[] };
        manualMapping = data.targets.find(target => target.manual_mapping)?.ams_mapping;
        return HttpResponse.json({ targets: data.targets.map(target => ({ ...target, status: 'incompatible', mapping: null,
          reason: { code: 'color_mismatch', message: 'The loaded colour does not match.' } })) });
      }));
    openQueueDialog({ initialRouting: routing({ force_color_match: true }) });
    await screen.findByTestId('feasibility-notice');
    await user.selectOptions(await screen.findByTitle('Auto-matched'), '0');
    await waitFor(() => expect(manualMapping).toEqual([0]));
    expect(await submitButton()).toBeDisabled();
    expect(queuePosts).toBe(0);
  });

  it('shows settled unknown BEFORE any silent auto-queue POST', async () => {
    let autoPosts = 0;
    server.use(needs(oneAbsChannel),
      http.post('/api/v1/auto-queue/routing-preview', () => HttpResponse.json(previewWith([group({ unknown: 1 })]))),
      http.post('/api/v1/auto-queue/', () => { autoPosts++; return HttpResponse.json({ id: 9 }); }));
    openQueueDialog({ initialDispatchMode: 'auto', initialSelectedPrinterIds: undefined,
      autoSubmitWhenUnambiguous: true, onAutoSubmitRefused });
    await waitFor(() => expect(onAutoSubmitRefused).toHaveBeenCalled());
    expect(autoPosts).toBe(0);
    expect(await submitButton()).toBeDisabled();
    expect(screen.getByTestId('feasibility-notice')).toHaveTextContent(/not fully verified/i);
    expect(screen.getByTestId('feasibility-override')).toBeEnabled();
  });

  it('checks every actual target in a fan-out, including an incompatible model', async () => {
    let targets: number[] = [];
    server.use(needs(oneAbsChannel),
      http.get('/api/v1/printers/', () => HttpResponse.json([...printers, { ...printers[0], id: 2, model: 'A1' }])),
      http.post('/api/v1/auto-queue/printer-routing-preview', async ({ request }) => {
        const data = await request.json() as { targets: { printer_id: number; plate_id: number }[] };
        targets = data.targets.map(target => target.printer_id);
        return HttpResponse.json({ targets: data.targets.map(target => ({ ...target,
          status: target.printer_id === 2 ? 'incompatible' : 'compatible', mapping: target.printer_id === 2 ? null : [0],
          reason: target.printer_id === 2 ? { code: 'model_mismatch', message: 'The sliced printer model does not match.' } : null })) });
      }));
    openQueueDialog({ initialSelectedPrinterIds: [1, 2] });
    await screen.findByTestId('feasibility-notice');
    expect(targets).toEqual([1, 2]);
    expect(screen.getByTestId('feasibility-notice').closest('form')?.querySelector('button[type="submit"]')).toBeDisabled();
    expect(screen.queryByTestId('feasibility-override')).not.toBeInTheDocument();
  });

  it('disables the button, names the reason and offers an override when a used channel has no source', async () => {
    backendRefusal();
    const user = userEvent.setup();
    server.use(
      needs(oneAbsChannel),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }])),
      ),
    );

    openQueueDialog();

    const notice = await screen.findByTestId('feasibility-notice');
    expect(notice).toHaveTextContent(/no compatible filament source is available/i);
    expect(notice).toHaveTextContent(/channel 1 needs ABS/i);
    expect(await submitButton()).toBeDisabled();

    await user.click(screen.getByTestId('feasibility-override'));

    const dialog = await screen.findByRole('dialog', { name: /queue it anyway/i });
    expect(queuePosts).toBe(0);

    await user.click(within(dialog).getByRole('button', { name: /queue anyway/i }));

    await waitFor(() => expect(queuePosts).toBe(1));
  });

  it('⚠️ blocks a strict-colour channel with no source, which reports `type_only`', async () => {
    backendRefusal('color_mismatch', 'The required color is not loaded.');
    // The first design read the channel's status word: `type_only` was taken to
    // mean "a spool was found, only the colour is off". Under a strict colour it
    // means the opposite — nothing was assigned at all and the mapping holds -1.
    server.use(
      needs([{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }]),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: '00FF00FF' }])),
      ),
    );

    openQueueDialog({ initialRouting: routing({ force_color_match: true }) });

    const notice = await screen.findByTestId('feasibility-notice');
    expect(notice).toHaveTextContent(/the required color is not loaded/i);
    expect(await submitButton()).toBeDisabled();
  });

  it('⚠️ names the PROFILE when the option is off and the material is in the tray', async () => {
    backendRefusal('variant_mismatch', 'The filament variant does not match. Channel 1 needs PETG (Pa240002); loaded: PETG (GFG99).');
    // The mirror counter-example: `mismatch` is also the answer for a pure
    // profile veto, and «No compatible filament source» would be a lie about a
    // machine holding exactly the right material.
    server.use(
      needs([{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10, tray_info_idx: 'Pa240002' }]),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF', tray_info_idx: 'GFG99' }])),
      ),
    );

    openQueueDialog({ initialRouting: routing({ allow_base_material_match: false }) });

    const notice = await screen.findByTestId('feasibility-notice');
    expect(notice).toHaveTextContent(/the filament variant does not match/i);
    expect(notice).toHaveTextContent(/Pa240002/);
    expect(notice).toHaveTextContent(/GFG99/);
    expect(notice).not.toHaveTextContent(/no compatible filament source/i);
  });

  it('leaves the button alive for a printer that is merely busy', async () => {
    server.use(
      needs(oneAbsChannel),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'ABS', tray_color: 'FF0000FF' }], { state: 'RUNNING' })),
      ),
    );

    openQueueDialog();

    const submit = await submitButton();
    await waitFor(() => expect(submit).toBeEnabled());
    expect(screen.queryByTestId('feasibility-notice')).not.toBeInTheDocument();
  });

  it('uses a compatible resolver assignment even when the separate display status request fails', async () => {
    // The display endpoint is not the authority. The default preview handler
    // confirms a complete assignment independently of this failed request.
    server.use(
      needs(oneAbsChannel),
      http.get('/api/v1/printers/:id/status', () => new HttpResponse(null, { status: 503 })),
    );

    openQueueDialog();

    const submit = await submitButton();
    await waitFor(() => expect(submit).toBeEnabled());
    expect(screen.queryByTestId('feasibility-notice')).not.toBeInTheDocument();
  });

  it('disables with no override when the file was sliced for another model', async () => {
    backendRefusal('model_mismatch', 'The sliced printer model does not match.');
    server.use(
      http.get('/api/v1/archives/:id', () => HttpResponse.json({ id: 1, sliced_for_model: 'A1' })),
      needs([{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }]),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }])),
      ),
    );

    openQueueDialog();

    const notice = await screen.findByTestId('feasibility-notice');
    expect(notice).toHaveTextContent(/sliced printer model does not match/i);
    expect(await submitButton()).toBeDisabled();
    expect(screen.queryByTestId('feasibility-override')).not.toBeInTheDocument();
  });

  it('⚠️ does not call two models of one G-code family a mismatch', async () => {
    // The machine takes an X1C plate on a P1S (`exact_model=False` for a job
    // queued to a chosen printer), and a refusal about the TARGET has no
    // override — a plain `!==` here would be a dead end in the dialog for a
    // print that would have run.
    server.use(
      http.get('/api/v1/archives/:id', () => HttpResponse.json({ id: 1, sliced_for_model: 'P1S' })),
      needs([{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }]),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }])),
      ),
    );

    openQueueDialog();

    const submit = await submitButton();
    await waitFor(() => expect(submit).toBeEnabled());
    expect(screen.queryByTestId('feasibility-notice')).not.toBeInTheDocument();
  });

  it('⚠️ leaves the legacy «sliced for» banner out for two models of one family', async () => {
    // The banner predates the verdict and asked a raw `!==`. An X1C plate on a
    // P1S would show a yellow warning while the feasibility block beside it
    // said nothing at all — two answers to one question, in one dialog.
    server.use(
      http.get('/api/v1/printers/', () =>
        HttpResponse.json([{ ...printers[0], name: 'P1S', model: 'P1S' }]),
      ),
      http.get('/api/v1/archives/:id', () => HttpResponse.json({ id: 1, sliced_for_model: 'X1C' })),
      needs([{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }]),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }])),
      ),
    );

    openQueueDialog();

    const submit = await submitButton();
    await waitFor(() => expect(submit).toBeEnabled());
    expect(screen.queryByText(/was sliced for/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('feasibility-notice')).not.toBeInTheDocument();
  });

  it('⚠️ refuses the form’s own submit too — the disabled button is not the gate', async () => {
    backendRefusal();
    // Enter in a field submits the FORM; `runSubmit` is reached by that event
    // without passing through the button at all, and a grouped run's silent
    // member never renders a button in the first place. So the verdict is
    // re-asked inside `runSubmit`, and this drives the form event directly:
    // jsdom suppresses implicit submission while the default button is
    // disabled, which would test the button rather than the guard.
    server.use(
      needs(oneAbsChannel),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }])),
      ),
    );

    openQueueDialog();

    await screen.findByTestId('feasibility-notice');
    await waitFor(() => expect(screen.getByTestId('feasibility-notice').closest('form')).not.toBeNull());
    fireEvent.submit(screen.getByTestId('feasibility-notice').closest('form')!);

    await waitFor(() => expect(screen.getByTestId('feasibility-notice')).toBeInTheDocument());
    expect(queuePosts).toBe(0);
    expect(onSuccess).not.toHaveBeenCalled();
  });

  describe('auto mode reads the preview, and reads it three-valued', () => {
    const openAuto = (preview: RoutingPreview) => {
      server.use(
        needs(oneAbsChannel),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }])),
        ),
        http.post('/api/v1/auto-queue/routing-preview', () => HttpResponse.json(preview)),
      );
      return openQueueDialog({ initialDispatchMode: 'auto', initialSelectedPrinterIds: undefined });
    };

    it('blocks when something is incompatible and nothing is compatible or unknown', async () => {
      openAuto(
        previewWith([
          group({
            incompatible: 1,
            reasons: [{ code: 'material_mismatch', message: 'No compatible filament source is available.', count: 1 }],
          }),
        ]),
      );

      const notice = await screen.findByTestId('feasibility-notice');
      expect(notice).toHaveTextContent(/no compatible filament source is available/i);
      expect(await submitButton()).toBeDisabled();
      expect(screen.getByTestId('feasibility-override')).toBeInTheDocument();
    });

    it('asks for explicit waiting when every printer is offline, without claiming incompatibility', async () => {
      openAuto(previewWith([group({ unknown: 2, total: 2 })]));

      const submit = await submitButton();
      expect(await screen.findByTestId('feasibility-notice')).toHaveTextContent(/not fully verified/i);
      expect(submit).toBeDisabled();
      expect(screen.getByTestId('feasibility-override')).toBeEnabled();
    });

    it('requires explicit waiting for incomplete evaluation, not a false incompatibility verdict', async () => {
      // One incompatible plus one printer whose snapshot failed reads as
      // `compatible=0, unknown=0, incompatible=1` — counters that look complete
      // while the evaluation is not.
      openAuto(
        previewWith(
          [
            group({
              incompatible: 1,
              reasons: [{ code: 'material_mismatch', message: 'No compatible filament source is available.', count: 1 }],
            }),
          ],
          true,
        ),
      );

      const submit = await submitButton();
      expect(await screen.findByTestId('feasibility-notice')).toHaveTextContent(/not fully verified/i);
      expect(submit).toBeDisabled();
      expect(screen.getByTestId('feasibility-override')).toBeEnabled();
    });

    it('leaves the button alive when nothing is READY but something is compatible', async () => {
      openAuto(previewWith([group({ compatible: 2, total: 2, ready: 0 })]));

      const submit = await submitButton();
      await waitFor(() => expect(submit).toBeEnabled());
      expect(screen.queryByTestId('feasibility-notice')).not.toBeInTheDocument();
    });

    it('shows the unknown counter and qualifies a partial profile reason', async () => {
      openAuto(previewWith([group({ unknown: 1, reasons: [
        { code: 'variant_mismatch', message: 'Channel 1 needs PETG (P8e36324).', count: 1 },
      ] })]));
      const notice = await screen.findByTestId('feasibility-notice');
      expect(notice).toHaveTextContent(/not fully verified/i);
      expect(notice).toHaveTextContent(/P8e36324/);
      expect(screen.getByText(/Incompatible: 0 · Unverified: 1/)).toBeInTheDocument();
      expect(await submitButton()).toBeDisabled();
    });

    it('requires a separate future-queue action when the farm has no target model', async () => {
      const user = userEvent.setup();
      let autoPosts = 0;
      server.use(http.post('/api/v1/auto-queue/', () => { autoPosts++; return HttpResponse.json({ id: 9 }); }));
      const preview = previewWith([]);
      preview.plates[0].model = 'P2S';
      openAuto(preview);
      const notice = await screen.findByTestId('feasibility-notice');
      expect(notice).toHaveTextContent(/no active P2S printers/i);
      expect(notice).not.toHaveTextContent(/not fully verified/i);
      expect(await submitButton()).toBeDisabled();
      fireEvent.submit(screen.getByTestId('feasibility-notice').closest('form')!);
      expect(autoPosts).toBe(0);
      await user.click(screen.getByRole('button', { name: /save in AutoQueue for the future/i }));
      const confirmation = await screen.findByRole('dialog', { name: /queue it anyway/i });
      expect(confirmation).toHaveTextContent(/does not make it printable/i);
      expect(autoPosts).toBe(0);
      await user.click(within(confirmation).getByRole('button', { name: /save in AutoQueue for the future/i }));
      await waitFor(() => expect(autoPosts).toBe(1));
    });

    it('does not claim a missing model when all snapshots failed', async () => {
      openAuto(previewWith([], true));
      expect(await screen.findByTestId('feasibility-notice')).toHaveTextContent(/not fully verified/i);
      expect(screen.queryByText(/no active .* printers/i)).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /save in AutoQueue for the future/i })).not.toBeInTheDocument();
      expect(await submitButton()).toBeDisabled();
    });
  });

  it('⚠️ shows itself instead of hanging a silent group run on an unprintable member', async () => {
    backendRefusal();
    // The sequencer advances only on `onClose`. A hidden member that neither
    // submits nor renders stops the whole run with a blank screen.
    server.use(
      needs([{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }]),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: '00FF00FF' }])),
      ),
    );

    openQueueDialog({
      autoSubmitWhenUnambiguous: true,
      initialRouting: routing({ force_color_match: true }),
      onAutoSubmitRefused,
    });

    expect(await screen.findByTestId('feasibility-notice')).toBeInTheDocument();
    expect(queuePosts).toBe(0);
    await waitFor(() => expect(onAutoSubmitRefused).toHaveBeenCalled());
  });

  it('never blocks saving an edit of a job already in a queue', async () => {
    backendRefusal();
    server.use(
      needs(oneAbsChannel),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }])),
      ),
    );

    render(
      <PrintModal
        mode="edit-queue-item"
        archiveId={1}
        archiveName="Bracket"
        queueItem={queueItem()}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    );

    const save = await screen.findByRole('button', { name: /^save$/i });
    await waitFor(() => expect(save).toBeEnabled());
    // The verdict is still told, as information — it just does not hold the form.
    expect(await screen.findByTestId('feasibility-notice')).toBeInTheDocument();
    expect(screen.queryByTestId('feasibility-override')).not.toBeInTheDocument();
  });

  it('⚠️ queues the job instead of repeating the direct print when «print now» is overridden', async () => {
    backendRefusal();
    // A direct print that has to wait for filament ends `cancelled`
    // (`defer_claim(direct=True)`), so the override has to write a queue row.
    const user = userEvent.setup();
    server.use(
      needs(oneAbsChannel),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusWith([{ tray_type: 'PETG', tray_color: 'FF0000FF' }])),
      ),
    );

    render(
      <PrintModal
        mode="reprint"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={[1]}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    );

    await screen.findByTestId('feasibility-notice');
    expect(await screen.findByRole('button', { name: /^print$/i })).toBeDisabled();

    await user.click(screen.getByTestId('feasibility-override'));
    const dialog = await screen.findByRole('dialog', { name: /queue it anyway/i });
    await user.click(within(dialog).getByRole('button', { name: /add to the queue/i }));

    await waitFor(() => expect(queuePosts).toBe(1));
    expect(reprints).toBe(0);
  });
});
