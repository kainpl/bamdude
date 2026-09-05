/**
 * The pre-print filament check against AMS Filament Backup.
 *
 * With backup on the printer swaps to another loaded tray of the same filament
 * and colour when the running one runs out, so two half spools cover a print
 * neither of them covers alone. Weighing each tray on its own warned about
 * every such print — and a warning the operator learns to click past is worse
 * than none, because the one that matters looks the same.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const printers = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
];

/** Two AMS trays of the identical filament — what backup can swap between. */
const twinTrays = (overrides: { backup: boolean; remainA?: number; remainB?: number }) => ({
  connected: true,
  state: 'IDLE',
  ams_auto_switch_filament: overrides.backup,
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: 'PLA', tray_color: 'FF0000FF', tray_info_idx: 'GFA00', remain: overrides.remainA ?? 100 },
        { id: 1, tray_type: 'PLA', tray_color: 'FF0000FF', tray_info_idx: 'GFA00', remain: overrides.remainB ?? 100 },
      ],
    },
  ],
  vt_tray: [],
});

/** An inventory spool with `grams` left of its 1 kg label weight. */
const assignment = (id: number, trayId: number, grams: number) => ({
  id,
  spool_id: id,
  printer_id: 1,
  printer_name: 'X1 Carbon',
  ams_id: 0,
  tray_id: trayId,
  fingerprint_color: null,
  fingerprint_type: null,
  configured: true,
  created_at: '2026-09-05T00:00:00Z',
  spool: { id, label_weight: 1000, weight_used: 1000 - grams },
});

describe('the filament warning and AMS Filament Backup', () => {
  let reprints: number;
  let assignmentsServed: number;
  const onClose = vi.fn<() => void>();
  const onSuccess = vi.fn<() => void>();

  /**
   * The assignments handler, wrapped so a test can wait for the answer to have
   * been served. ⚠️ Submitting before it lands would find no spool to weigh and
   * pass silently — which is why the backup-off twin of the first case exists:
   * it warns on the identical timing, so a "no warning" here means the check
   * ran and said yes, not that it never ran.
   */
  const serveAssignments = (rows: unknown[]) =>
    http.get('/api/v1/inventory/assignments', () => {
      assignmentsServed += 1;
      return HttpResponse.json(rows);
    });

  beforeEach(() => {
    vi.clearAllMocks();
    reprints = 0;
    assignmentsServed = 0;
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/archives/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] })
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () =>
        HttpResponse.json({
          filaments: [{ slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 300, tray_info_idx: 'GFA00' }],
        })
      ),
      // Tray 0 alone cannot cover the plate; tray 0 + tray 1 comfortably can.
      serveAssignments([assignment(1, 0, 200), assignment(2, 1, 800)]),
      http.post('/api/v1/archives/:id/reprint', () => {
        reprints += 1;
        return HttpResponse.json({ status: 'started', archive_id: 1 });
      })
    );
  });

  const mountAndSubmit = async () => {
    const user = userEvent.setup();
    render(
      <PrintModal
        mode="reprint"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={[1]}
        onClose={onClose}
        onSuccess={onSuccess}
      />
    );
    // The check reads the printer status, the requirements and the
    // assignments; submitting before they land would pass for the wrong reason.
    await waitFor(() => expect(assignmentsServed).toBeGreaterThan(0));
    const submit = await screen.findByRole('button', { name: /^print$/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    return user;
  };

  it('does not warn when a second spool of the same filament covers the shortfall', async () => {
    server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json(twinTrays({ backup: true }))));

    await mountAndSubmit();

    await waitFor(() => expect(reprints).toBe(1));
    expect(screen.queryByText(/not enough filament/i)).not.toBeInTheDocument();
  });

  it('⚠️ still warns per tray when backup is off — the printer would not swap', async () => {
    server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json(twinTrays({ backup: false }))));

    await mountAndSubmit();

    expect(await screen.findByText(/not enough filament/i)).toBeInTheDocument();
    expect(screen.getByText(/X1 Carbon - A1: needs 300g, remaining 200g/)).toBeInTheDocument();
    expect(reprints).toBe(0);
  });

  it('lets an unregistered tray pay into the pool from its AMS fill percentage', async () => {
    // Tray 1 is in no inventory; the AMS says 60 % of a nominal 1 kg reel, so
    // the group holds 200 + 600 g against a 300 g plate.
    server.use(
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(twinTrays({ backup: true, remainB: 60 }))
      ),
      serveAssignments([assignment(1, 0, 200)])
    );

    await mountAndSubmit();

    await waitFor(() => expect(reprints).toBe(1));
    expect(screen.queryByText(/not enough filament/i)).not.toBeInTheDocument();
  });

  it('⚠️ warns — naming the whole group — when the unregistered tray reports nothing usable', async () => {
    // `remain: 0` is the firmware's "no answer", never "empty" (backend
    // `utils/filament_remaining`), so it pays in nothing and the pool is the
    // 200 g of tray 0 alone.
    server.use(
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(twinTrays({ backup: true, remainB: 0 }))
      ),
      serveAssignments([assignment(1, 0, 200)])
    );

    await mountAndSubmit();

    expect(await screen.findByText(/not enough filament/i)).toBeInTheDocument();
    expect(
      screen.getByText(/X1 Carbon - A1, A2 \(backup group\): needs 300g, remaining 200g/)
    ).toBeInTheDocument();
    expect(reprints).toBe(0);
  });

  it('⚠️ with backup OFF an unregistered tray is still silent — the fill percentage pays nothing', async () => {
    // The nominal-reel fallback is pinned to backup-on. Without that pin it
    // would fire here too and invent a warning nobody used to get: a farm that
    // does not register every tray in inventory would start being told a slot
    // is short on the strength of a guessed reel size. Trays 0 and 1 are the
    // same filament and both unregistered; tray 2 carries the one assignment,
    // which is what keeps the check running at all.
    server.use(
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({
          connected: true,
          state: 'IDLE',
          ams_auto_switch_filament: false,
          ams: [
            {
              id: 0,
              tray: [
                { id: 0, tray_type: 'PLA', tray_color: 'FF0000FF', tray_info_idx: 'GFA00', remain: 45 },
                { id: 1, tray_type: 'PLA', tray_color: 'FF0000FF', tray_info_idx: 'GFA00', remain: 45 },
                { id: 2, tray_type: 'PETG', tray_color: '00FF00FF', tray_info_idx: 'GFG00', remain: 100 },
              ],
            },
          ],
          vt_tray: [],
        })
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () =>
        HttpResponse.json({
          filaments: [{ slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 500, tray_info_idx: 'GFA00' }],
        })
      ),
      serveAssignments([assignment(3, 2, 900)])
    );

    await mountAndSubmit();

    await waitFor(() => expect(reprints).toBe(1));
    expect(screen.queryByText(/not enough filament/i)).not.toBeInTheDocument();
  });

  it('⚠️ sums two plates that both draw on the group — one pool, two demands', async () => {
    // 200 g per plate against a 350 g pool: either plate fits, the pair does
    // not, and a per-plate check would have passed both.
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(twinTrays({ backup: true }))),
      http.get('/api/v1/archives/:id/plates', () =>
        HttpResponse.json({
          is_multi_plate: true,
          plates: [
            { index: 1, name: 'Plate 1', objects: ['Part A'], filaments: [{ type: 'PLA', color: '#FF0000' }] },
            { index: 2, name: 'Plate 2', objects: ['Part B'], filaments: [{ type: 'PLA', color: '#FF0000' }] },
          ],
        })
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () =>
        HttpResponse.json({
          filaments: [{ slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 200, tray_info_idx: 'GFA00' }],
        })
      ),
      serveAssignments([assignment(1, 0, 200), assignment(2, 1, 150)])
    );

    const user = userEvent.setup();
    render(
      <PrintModal
        mode="reprint"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={[1]}
        preselectedPlateIds={[1, 2]}
        onClose={onClose}
        onSuccess={onSuccess}
      />
    );

    await waitFor(() => expect(assignmentsServed).toBeGreaterThan(0));
    const submit = await screen.findByRole('button', { name: /^print$/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    expect(await screen.findByText(/not enough filament/i)).toBeInTheDocument();
    expect(
      screen.getByText(/X1 Carbon - A1, A2 \(backup group\): needs 400g, remaining 350g/)
    ).toBeInTheDocument();
    expect(reprints).toBe(0);
  });
});
