/**
 * Load and Unload on an AMS-HT slot, and which column a unit is drawn in
 * (upstream 5a9415e2e, cacaaf4da leg 6).
 *
 * An AMS-HT tray is its unit id (128-135), as tray_now reports it — the card
 * used to have no Load / Unload for it at all. And a unit is an AMS-HT by its
 * id, never because it listed a single tray.
 */
import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockPrinter = {
  id: 1,
  name: 'H2D',
  ip_address: '192.168.1.100',
  serial_number: '0948BB540200427',
  access_code: '12345678',
  model: 'H2D',
  enabled: true,
  nozzle_count: 2,
  nozzle_diameter: 0.4,
  nozzle_type: 'hardened_steel',
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const tray = {
  tray_color: 'FF0000FF', tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_id_name: 'A00-R0',
  tray_info_idx: 'GFA00', remain: 80, k: 0.02, cali_idx: null, tag_uid: null, tray_uuid: null,
  nozzle_temp_min: 190, nozzle_temp_max: 230, drying_temp: 55, drying_time: 8, state: 3,
};

const HT_UNIT = { id: 128, humidity: 20, temp: 40, is_ams_ht: true, serial_number: 'HT00', sw_ver: '03.00.21.29',
  module_type: 'n3s', tray: [{ id: 0, ...tray }] };

function makeStatus(over: Record<string, unknown>) {
  return {
    connected: true, state: 'IDLE', progress: 0, layer_num: 0, total_layers: 0,
    temperatures: { nozzle: 25, nozzle_2: 25, bed: 25, chamber: 25 },
    remaining_time: 0, filename: null, wifi_signal: -29, speed_level: 2, vt_tray: [],
    ams: [HT_UNIT],
    ams_extruder_map: { '128': 0 }, fila_switch: null, ams_switch_inlet: {}, extruder_slots: {},
    ...over,
  };
}

function renderWith(over: Record<string, unknown> = {}) {
  const status = makeStatus(over);
  const posted: string[] = [];
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, status])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.post('/api/v1/printers/:id/ams/load', ({ request }) => {
      posted.push(`load${new URL(request.url).search}`);
      return HttpResponse.json({ success: true, tray_id: 128 });
    }),
    http.post('/api/v1/printers/:id/ams/unload', ({ request }) => {
      posted.push(`unload${new URL(request.url).search}`);
      return HttpResponse.json({ success: true });
    }),
  );
  render(<PrintersPage />);
  return posted;
}

async function openFirstSlotMenu() {
  const buttons = await screen.findAllByTitle('Slot options', {}, { timeout: 5000 });
  fireEvent.click(buttons[0]);
}

describe('PrintersPage — Load / Unload on an AMS-HT slot', () => {
  it('loads the tray by its unit id', async () => {
    const posted = renderWith();
    await openFirstSlotMenu();
    fireEvent.click(await screen.findByRole('button', { name: /Load filament/ }));

    await waitFor(() => expect(posted).toEqual(['load?tray_id=128']));
  });

  it('unloads the tray by its unit id', async () => {
    const posted = renderWith();
    await openFirstSlotMenu();
    fireEvent.click(await screen.findByRole('button', { name: /Unload filament/ }));

    await waitFor(() => expect(posted).toEqual(['unload?tray_id=128']));
  });

  it('asks which hotend to feed behind a Filament Track Switch', async () => {
    const posted = renderWith({
      fila_switch: { installed: true, in_slots: [-1, -1], out_extruders: [1, 0], stat: 0, info: 0, ready: true },
      ams_switch_inlet: { '128': 'A' },
      ams_extruder_map: {},
    });
    await openFirstSlotMenu();
    fireEvent.click(await screen.findByRole('button', { name: /Load filament/ }));

    expect(await screen.findByText('Load HT-A to which nozzle?')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Left nozzle/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    await waitFor(() => expect(posted).toEqual(['load?tray_id=128&extruder_id=1']));
  });
});

describe('PrintersPage — a unit is an AMS-HT by its id', () => {
  it('draws a regular AMS that listed one tray as a regular AMS', async () => {
    renderWith({
      ams: [{ id: 1, humidity: 30, temp: 33, is_ams_ht: false, serial_number: 'AMS01', sw_ver: '03.00.21.29',
        module_type: 'n3f', tray: [{ id: 2, ...tray }] }],
      ams_extruder_map: { '1': 0 },
    });

    expect(await screen.findByText('AMS-B', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText('HT-B')).toBeNull();
    // All four slots of the unit are drawn, each with its menu.
    expect(await screen.findAllByTitle('Slot options')).toHaveLength(4);
  });
});
