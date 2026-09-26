/**
 * A switched-off printer with its plate gate up offers Clear plate
 * (upstream 05d87a97, #2864).
 *
 * With Auto Power Off the ordinary end of a print is the gate up and the
 * printer off. The card drew the plate buttons only inside its live-status
 * body, so nothing on the page could release the gate until somebody switched
 * the printer back on.
 */
import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const printer = {
  id: 1, name: 'Sleeper', ip_address: '192.168.1.100', serial_number: '01P00A000000001', access_code: '12345678',
  model: 'P1S', enabled: true, is_active: true, require_plate_clear: true, nozzle_diameter: 0.4,
  nozzle_type: 'stainless_steel', location: 'Workshop', auto_archive: true,
  created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
};

function renderWith(size: '1' | '3') {
  localStorage.setItem('printerCardSize', size);
  const status = { id: 1, name: 'Sleeper', connected: false, awaiting_plate_clear: true };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, status])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
  );
  render(<PrintersPage />);
}

describe('PrintersPage — Clear plate on a switched-off printer', () => {
  it('is offered on the expanded card', async () => {
    renderWith('3');
    expect(await screen.findByTitle('Mark plate as cleared')).toBeInTheDocument();
  });

  it('is offered on the compact card', async () => {
    renderWith('1');
    expect(await screen.findByTitle('Mark plate as cleared')).toBeInTheDocument();
  });
});
