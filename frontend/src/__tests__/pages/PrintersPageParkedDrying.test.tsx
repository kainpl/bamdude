/**
 * A parked drying timer on the printer card (upstream #2896 / #3096): the
 * printer took the command, keeps the countdown and is not heating.
 */
import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const printer = {
  id: 1, name: 'H2D', ip_address: '192.168.1.100', serial_number: '00M09A350100001', access_code: '12345678',
  model: 'H2D', enabled: true, nozzle_count: 1, nozzle_diameter: 0.4, nozzle_type: 'hardened_steel',
  location: 'Workshop', auto_archive: true, created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
};
const tray = {
  tray_color: 'FF0000FF', tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_id_name: 'A00-R0', tray_info_idx: 'GFA00',
  remain: 80, k: 0.02, cali_idx: null, tag_uid: null, tray_uuid: null, nozzle_temp_min: 190, nozzle_temp_max: 230,
  drying_temp: 55, drying_time: 8, state: 3,
};
const standard = (unit: Record<string, unknown>) => ({
  id: 0, humidity: 30, temp: 33, is_ams_ht: false, serial_number: 'AMS00', sw_ver: '1', module_type: 'n3f',
  dry_time: 0, dry_status: 0, dry_sf_reason: [], dry_filament: 'PLA', dry_target_temp: 45,
  tray: [0, 1, 2, 3].map((id) => ({ id, ...tray })), ...unit,
});
const ht = (unit: Record<string, unknown>) => ({
  id: 128, humidity: 30, temp: 33, is_ams_ht: true, serial_number: 'HT00', sw_ver: '1', module_type: 'n3s',
  dry_time: 0, dry_status: 0, dry_sf_reason: [], dry_filament: 'PA', dry_target_temp: 85,
  tray: [{ id: 0, ...tray }], ...unit,
});
const PARKED = { dry_time: 720, dry_countdown_stalled: true };
const TICKING = { dry_time: 720, dry_status: 2, dry_countdown_stalled: false };

function mount(ams: Record<string, unknown>[]) {
  const posted: string[] = [];
  const s = {
    connected: true, state: 'IDLE', progress: 0, layer_num: 0, total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 }, remaining_time: 0, filename: null, wifi_signal: -29,
    speed_level: 2, vt_tray: [], supports_drying: true, ams_extruder_map: {}, fila_switch: null, ams,
  };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(s)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, s])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/scheduled-dryings', () => HttpResponse.json([])),
    http.get('/api/v1/drying-schedules', () => HttpResponse.json({ server_timezone: 'Europe/Kyiv', schedules: [] })),
    http.post('/api/v1/printers/1/drying/start', () => {
      posted.push('now');
      return HttpResponse.json({ status: 'drying_started', ams_id: 0, temp: 55, duration: 8 });
    }),
    http.post('/api/v1/printers/1/drying/stop', () => {
      posted.push('stop');
      return HttpResponse.json({ status: 'drying_stopped', ams_id: 0 });
    }),
  );
  render(<PrintersPage />);
  return posted;
}

describe('PrintersPage — a parked drying timer', () => {
  it('a standard AMS shows "Drying not running", with the reason and no countdown', async () => {
    mount([standard(PARKED)]);
    const bar = await screen.findByTestId('ams-drying-parked-0');
    expect(within(bar).getByText('Drying not running')).toBeInTheDocument();
    expect(bar.getAttribute('title')).toMatch(/holds a drying timer/);
    expect(within(bar).getByText('PLA @ 45°C')).toBeInTheDocument();
    expect(screen.queryByText(/12h 0m/)).not.toBeInTheDocument();
    expect(screen.queryByText('Drying')).not.toBeInTheDocument();
  });

  it('the timer can still be taken off', async () => {
    const posted = mount([standard(PARKED)]);
    const bar = await screen.findByTestId('ams-drying-parked-0');
    fireEvent.click(within(bar).getByTitle('Stop Drying'));
    await waitFor(() => expect(posted).toEqual(['stop']));
  });

  it('an AMS-HT shows it too', async () => {
    mount([ht(PARKED)]);
    const bar = await screen.findByTestId('ams-drying-parked-128');
    expect(within(bar).getByText('Drying not running')).toBeInTheDocument();
    expect(within(bar).getByTitle('Stop Drying')).toBeInTheDocument();
    expect(screen.queryByText(/12h 0m/)).not.toBeInTheDocument();
  });

  it('a countdown that is ticking keeps the active bar on both kinds', async () => {
    mount([standard(TICKING), ht(TICKING)]);
    expect((await screen.findAllByText(/12h 0m/)).length).toBe(2);
    expect(screen.queryByText('Drying not running')).not.toBeInTheDocument();
  });

  it('the popover treats a parked unit as not drying: "Now" starts and replaces the timer', async () => {
    const posted = mount([standard(PARKED)]);
    fireEvent.click(await screen.findByTitle('Start Drying'));
    const popover = await screen.findByTestId('ams-drying-popover');
    expect(within(popover).getByRole('button', { name: 'Now' })).not.toBeDisabled();
    fireEvent.click(within(popover).getByTestId('drying-start-confirm'));
    await waitFor(() => expect(posted).toEqual(['now']));
  });
});
