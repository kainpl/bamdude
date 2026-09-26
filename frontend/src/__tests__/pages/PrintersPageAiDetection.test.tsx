/**
 * The AI detection badge never says a print is watched when it is not
 * (upstream 06e5114a, #2952).
 *
 * A monitored print with no result yet, or whose last poll produced none (a
 * rejected ML token, a camera that gave no frame), fell through to grey "AI
 * Idle" — or, before the backend stopped defaulting to it, green "AI Safe".
 */
import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const printer = {
  id: 1, name: 'Watched', ip_address: '192.168.1.100', serial_number: '01P00A000000001', access_code: '12345678',
  model: 'P1S', enabled: true, is_active: true, nozzle_diameter: 0.4, nozzle_type: 'stainless_steel',
  location: 'Workshop', auto_archive: true, created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
};

function renderWith(entry: Record<string, unknown>) {
  const status = {
    connected: true, state: 'RUNNING', progress: 10, layer_num: 1, total_layers: 10,
    temperatures: { nozzle: 220, bed: 60, chamber: 30 }, remaining_time: 600, filename: 'a.gcode',
    wifi_signal: -40, vt_tray: [], ams: [],
  };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, status])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/obico/printer-status', () =>
      HttpResponse.json({ enabled: true, monitored_printers: null, per_printer: { 1: entry }, last_error: null })),
  );
  render(<PrintersPage />);
}

describe('PrintersPage — AI detection badge', () => {
  it('says "not checking", with the reason, when the last poll produced no verdict', async () => {
    renderWith({ class: 'error', frame_count: 0, score: 0, error: 'Obico ML API rejected the token.' });
    const badge = await screen.findByText('AI not checking');
    expect(badge.closest('button')).toHaveAttribute('title', expect.stringContaining('rejected the token'));
    expect(screen.queryByText('AI Safe')).toBeNull();
  });

  it('says "starting" while a watched print waits for its first result', async () => {
    renderWith({ class: 'unknown', frame_count: 0, score: 0, error: null });
    expect(await screen.findByText('AI starting')).toBeInTheDocument();
    expect(screen.queryByText('AI Idle')).toBeNull();
  });

  it('still shows a real verdict', async () => {
    renderWith({ class: 'safe', frame_count: 5, score: 0.01, error: null });
    expect(await screen.findByText('AI Safe')).toBeInTheDocument();
  });
});
