/**
 * Load and Unload from the AMS slot menu (upstream 9500c046).
 *
 * A Filament Track Switch makes both hotends reachable from every slot, so a
 * load has to name one: the menu asks (FeedDirectionModal), refuses up front
 * while the switch is not set up (BambuStudio's DevFilaSwitch::IsReady), and
 * stays a one-click load on every printer without a switch. Unload names the
 * slot, so a dual-nozzle printer unloads the hotend that holds it rather than
 * whatever the printer-wide tray_now names.
 */
import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockPrinter = {
  id: 1,
  name: 'H2C',
  ip_address: '192.168.1.100',
  serial_number: '31B8BP610600650',
  access_code: '12345678',
  model: 'H2C',
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

function makeStatus(over: Record<string, unknown>) {
  return {
    connected: true, state: 'IDLE', progress: 0, layer_num: 0, total_layers: 0,
    temperatures: { nozzle: 25, nozzle_2: 25, bed: 25, chamber: 25 },
    remaining_time: 0, filename: null, wifi_signal: -29, speed_level: 2, vt_tray: [],
    ams: [{ id: 0, humidity: 30, temp: 33, is_ams_ht: false, serial_number: 'AMS00', sw_ver: '03.00.21.29',
      module_type: 'n3f', tray: [0, 1, 2, 3].map((t) => ({ id: t, ...tray })) }],
    ams_extruder_map: {}, fila_switch: null, ams_switch_inlet: {}, extruder_slots: {},
    ...over,
  };
}

const FTS = { installed: true, in_slots: [-1, -1], out_extruders: [1, 0], stat: 0, info: 0 };

function renderWith(over: Record<string, unknown>) {
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
      return HttpResponse.json({ success: true, tray_id: 0 });
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
  const buttons = await screen.findAllByTitle('Slot options');
  fireEvent.click(buttons[0]);
}

describe('PrintersPage — AMS slot Load / Unload with a Filament Track Switch', () => {
  it('asks which hotend to feed when the switch is set up', async () => {
    const posted = renderWith({ fila_switch: { ...FTS, ready: true }, ams_switch_inlet: { '0': 'A' } });
    await openFirstSlotMenu();
    fireEvent.click(await screen.findByRole('button', { name: /Load filament/ }));

    expect(await screen.findByText('Load A1 to which nozzle?')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Left nozzle/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    await waitFor(() => expect(posted).toEqual(['load?tray_id=0&extruder_id=1']));
  });

  it('refuses before asking while the switch is not set up', async () => {
    const posted = renderWith({ fila_switch: { ...FTS, ready: false } });
    await openFirstSlotMenu();
    fireEvent.click(await screen.findByRole('button', { name: /Load filament/ }));

    expect(await screen.findByText(/Filament Track Switch is not set up yet/)).toBeInTheDocument();
    expect(screen.queryByText('Load A1 to which nozzle?')).toBeNull();
    expect(posted).toEqual([]);
  });

  it('loads in one click without a switch', async () => {
    const posted = renderWith({ ams_extruder_map: { '0': 0 } });
    await openFirstSlotMenu();
    fireEvent.click(await screen.findByRole('button', { name: /Load filament/ }));

    await waitFor(() => expect(posted).toEqual(['load?tray_id=0']));
    expect(screen.queryByText(/to which nozzle\?/)).toBeNull();
  });

  it('unloads the slot whose menu was used', async () => {
    const posted = renderWith({ ams_extruder_map: { '0': 0 } });
    await openFirstSlotMenu();
    fireEvent.click(await screen.findByRole('button', { name: /Unload filament/ }));

    await waitFor(() => expect(posted).toEqual(['unload?tray_id=0']));
  });
});
