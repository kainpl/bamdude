/**
 * The card's Skip Objects button after a restart mid-print (upstream cfecfa36).
 *
 * A running print always has at least one object, so a count of zero means
 * "not loaded yet", not "nothing to skip". Disabling on zero killed the only
 * control that opens the modal — and the modal's own fetch is what rebuilds the
 * list, so the print could never get it back. Exactly one object is the real
 * nothing-to-skip case.
 */
import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const printer = {
  id: 1, name: 'X1C', ip_address: '192.168.1.100', serial_number: '00M09A350100001', access_code: '12345678',
  model: 'X1C', enabled: true, nozzle_count: 1, nozzle_diameter: 0.4, nozzle_type: 'hardened_steel',
  location: 'Workshop', auto_archive: true, created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
};

function mount(objectCount: number) {
  const status = {
    connected: true, state: 'RUNNING', progress: 40, layer_num: 10, total_layers: 100,
    temperatures: { nozzle: 220, bed: 60, chamber: 30 }, remaining_time: 3600, filename: 'part.3mf',
    subtask_name: 'part', wifi_signal: -40, speed_level: 2, vt_tray: [], ams: [], ams_extruder_map: {},
    fila_switch: null, printable_objects_count: objectCount, skip_objects_supported: true,
  };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, status])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/printers/:id/print/objects', () =>
      HttpResponse.json({ objects: [], total: 0, skipped_count: 0, is_printing: true })),
  );
  render(<PrintersPage />);
}

describe('Skip Objects on the printer card', () => {
  it('stays usable while the object list is not loaded yet (count 0)', async () => {
    mount(0);
    expect(await screen.findByTitle('Skip objects')).toBeEnabled();
  });

  it('is off when the print has exactly one object', async () => {
    mount(1);
    expect(await screen.findByTitle('Skip objects (requires 2+ objects)')).toBeDisabled();
  });

  it('is usable with several objects', async () => {
    mount(3);
    expect(await screen.findByTitle('Skip objects')).toBeEnabled();
  });
});
