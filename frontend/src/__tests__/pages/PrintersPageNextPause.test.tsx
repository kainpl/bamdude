/**
 * The next slicer pause on the printer card (upstream 8901ff849, adapted).
 *
 * The server says where the next pause is (`next_pause`); the card only words
 * it, the same way in both card layouts: with a time when the server gave one,
 * with the layer alone otherwise, and nothing when the field is absent.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const printer = {
  id: 1, name: 'Pauser', ip_address: '192.168.1.100', serial_number: '01P00A000000001', access_code: '12345678',
  model: 'P1S', enabled: true, is_active: true, nozzle_diameter: 0.4,
  nozzle_type: 'stainless_steel', location: 'Workshop', auto_archive: true,
  created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
};

const printing = {
  id: 1, name: 'Pauser', connected: true, state: 'RUNNING', progress: 42, layer_num: 10, total_layers: 100,
  current_print: 'job.3mf', subtask_name: 'job', gcode_file: 'job.3mf',
  remaining_time: 60, temperatures: { nozzle: 220, bed: 60 }, vt_tray: [], speed_level: 2,
};

function renderWith(size: '1' | '3', nextPause: { layer: number; minutes: number | null } | null | undefined) {
  localStorage.setItem('printerCardSize', size);
  const status = nextPause === undefined ? printing : { ...printing, next_pause: nextPause };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, status])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
  );
  render(<PrintersPage />);
}

afterEach(() => localStorage.removeItem('printerCardSize'));

describe.each([['expanded', '3'], ['compact', '1']] as const)('PrintersPage — next slicer pause, %s card', (_name, size) => {
  it('says how long until the pause when the server gave a time', async () => {
    renderWith(size, { layer: 37, minutes: 12 });
    const line = await screen.findByTestId('next-pause', {}, { timeout: 10000 });
    expect(line).toHaveTextContent('Pause in ~12 min (layer 37)');
  }, 15000);

  it('says only the layer when there is no time', async () => {
    renderWith(size, { layer: 37, minutes: null });
    const line = await screen.findByTestId('next-pause', {}, { timeout: 10000 });
    expect(line).toHaveTextContent('Pause at layer 37');
  }, 15000);

  it('shows nothing when no pause is known', async () => {
    renderWith(size, null);
    expect(await screen.findByText('10/100', {}, { timeout: 10000 })).toBeInTheDocument();
    expect(screen.queryByTestId('next-pause')).not.toBeInTheDocument();
  }, 15000);

  it('shows nothing for a status without the field', async () => {
    renderWith(size, undefined);
    expect(await screen.findByText('10/100', {}, { timeout: 10000 })).toBeInTheDocument();
    expect(screen.queryByTestId('next-pause')).not.toBeInTheDocument();
  }, 15000);
});
