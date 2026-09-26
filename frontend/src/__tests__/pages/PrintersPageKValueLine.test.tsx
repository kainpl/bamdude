/**
 * The K-profile value is shown on the AMS slot card itself, not only in the
 * hover card (upstream 8d1daab2, #2854).
 *
 * The slot's `k` reaches the page already resolved per slot
 * (utils/kprofile_lookup.build_slot_k_resolver, both status shapers), so the
 * line only decides whether to show it:
 * - a missing value shows nothing: formatKValue() substitutes 0.020, which the
 *   hover card captions, but on a permanent uncaptioned line it would read as
 *   a measured K;
 * - a firmware "0" shows nothing either, and must not leak a bare "0" through
 *   a `k && …` guard (React renders numbers);
 * - when any slot on the card has a value, the slots without one reserve the
 *   row so every fill bar stays on one line.
 */
import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockPrinter = {
  id: 1,
  name: 'X1 Carbon',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'X1C',
  enabled: true,
  is_active: true,
  nozzle_diameter: 0.4,
  nozzle_type: 'hardened_steel',
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const EMPTY = { tray_type: null, state: 9 };

function renderWith(over: Record<string, unknown>) {
  const status = {
    connected: true, state: 'IDLE', progress: 0, layer_num: 0, total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 },
    remaining_time: 0, filename: null, wifi_signal: -50, vt_tray: [], ams: [],
    ...over,
  };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, status])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
  );
  render(<PrintersPage />);
}

/** The slot tile holding the material name: its own text, nothing else. */
function slotOf(material: string): HTMLElement {
  return screen.getByText(material).parentElement as HTMLElement;
}

describe('PrintersPage — K value on the AMS slot card', () => {
  it('shows the value on a loaded AMS slot without hovering, full name on the title', async () => {
    renderWith({
      ams: [{ id: 0, tray: [{ id: 0, tray_type: 'PETG', tray_color: 'FF0000FF', k: 0.024 }, { id: 1, ...EMPTY }, { id: 2, ...EMPTY }, { id: 3, ...EMPTY }] }],
    });
    await waitFor(() => expect(screen.getByText('K 0.024')).toBeInTheDocument());
    expect(screen.getByText('K 0.024')).toHaveAttribute('title', 'K Factor');
  });

  it('shows nothing on a card whose slots are all empty — no default 0.020 leaks out', async () => {
    renderWith({
      ams: [{ id: 0, tray: [0, 1, 2, 3].map((id) => ({ id, ...EMPTY })) }],
    });
    await waitFor(() => expect(screen.getAllByText('-').length).toBeGreaterThan(0));
    expect(screen.queryByText(/^K \d/)).toBeNull();
  });

  it('shows no value for a loaded slot the printer never calibrated (k null)', async () => {
    renderWith({
      ams: [{ id: 0, tray: [{ id: 0, tray_type: 'PETG', tray_color: 'FF0000FF', k: null }, { id: 1, ...EMPTY }, { id: 2, ...EMPTY }, { id: 3, ...EMPTY }] }],
    });
    await waitFor(() => expect(screen.getByText('PETG')).toBeInTheDocument());
    expect(screen.queryByText(/^K \d/)).toBeNull();
    expect(slotOf('PETG').children.length).toBe(3);
  });

  it('shows no value — and no stray "0" — when the printer reports exactly 0', async () => {
    renderWith({
      ams: [
        { id: 0, tray: [{ id: 0, tray_type: 'PETG', tray_color: 'FF0000FF', k: 0 }, { id: 1, ...EMPTY }, { id: 2, ...EMPTY }, { id: 3, ...EMPTY }] },
        { id: 128, tray: [{ id: 0, tray_type: 'ASA', tray_color: 'FFFFFFFF', k: 0 }] },
      ],
      vt_tray: [{ id: 254, tray_type: 'PLA', tray_color: '000000FF', k: 0 }],
    });
    await waitFor(() => expect(screen.getByText('ASA')).toBeInTheDocument());
    for (const material of ['PETG', 'ASA', 'PLA']) {
      expect(slotOf(material).textContent).not.toMatch(/0/);
    }
    expect(screen.queryByText(/^K \d/)).toBeNull();
  });

  it('shows the value on an AMS-HT slot', async () => {
    renderWith({
      ams: [{ id: 128, tray: [{ id: 0, tray_type: 'ASA', tray_color: 'FFFFFFFF', k: 0.018 }] }],
    });
    await waitFor(() => expect(screen.getByText('K 0.018')).toBeInTheDocument());
  });

  it('shows the value on the external spool', async () => {
    renderWith({
      vt_tray: [{ id: 254, tray_type: 'PLA', tray_color: '000000FF', k: 0.022 }],
    });
    await waitFor(() => expect(screen.getByText('K 0.022')).toBeInTheDocument());
  });

  it('reserves the row on slots without a value so the fill bars stay aligned', async () => {
    renderWith({
      ams: [{
        id: 0,
        tray: [
          { id: 0, tray_type: 'PETG', tray_color: 'FF0000FF', k: 0.024 },
          { id: 1, tray_type: 'PLA', tray_color: '00FF00FF', k: null },
          { id: 2, ...EMPTY },
          { id: 3, ...EMPTY },
        ],
      }],
    });
    await waitFor(() => expect(screen.getByText('K 0.024')).toBeInTheDocument());
    const calibrated = slotOf('PETG');
    const uncalibrated = slotOf('PLA');
    expect(uncalibrated.children.length).toBe(calibrated.children.length);
    expect(uncalibrated.textContent).not.toMatch(/K/);
  });
});
