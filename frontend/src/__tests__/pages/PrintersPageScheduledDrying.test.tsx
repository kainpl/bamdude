/** Scheduling a drying from the AMS popover (spec §UI). */
import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const printer = {
  id: 1, name: 'X1C', ip_address: '192.168.1.100', serial_number: '00M09A350100001', access_code: '12345678',
  model: 'X1C', enabled: true, nozzle_count: 1, nozzle_diameter: 0.4, nozzle_type: 'hardened_steel',
  location: 'Workshop', auto_archive: true, created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
};
const tray = {
  tray_color: 'FF0000FF', tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_id_name: 'A00-R0', tray_info_idx: 'GFA00',
  remain: 80, k: 0.02, cali_idx: null, tag_uid: null, tray_uuid: null, nozzle_temp_min: 190, nozzle_temp_max: 230,
  drying_temp: 55, drying_time: 8, state: 3,
};
function status(unit: Record<string, unknown> = {}) {
  return {
    connected: true, state: 'IDLE', progress: 0, layer_num: 0, total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 }, remaining_time: 0, filename: null, wifi_signal: -29,
    speed_level: 2, vt_tray: [], supports_drying: true, ams_extruder_map: {}, fila_switch: null,
    ams: [{ id: 0, humidity: 30, temp: 33, is_ams_ht: false, serial_number: 'AMS00', sw_ver: '1', module_type: 'n3f',
      dry_time: 0, dry_sf_reason: [], tray: [0, 1, 2, 3].map((id) => ({ id, ...tray })), ...unit }],
  };
}
function mount(unit: Record<string, unknown> = {}) {
  const posted: { path: string; body: unknown }[] = [];
  const s = status(unit);
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(s)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, s])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/scheduled-dryings', () => HttpResponse.json([])),
    http.get('/api/v1/drying-schedules', () => HttpResponse.json({ server_timezone: 'Europe/Kyiv', schedules: [] })),
    http.post('/api/v1/printers/1/drying/start', () => {
      posted.push({ path: 'now', body: null });
      return HttpResponse.json({ status: 'drying_started', ams_id: 0, temp: 55, duration: 8 });
    }),
    http.post('/api/v1/scheduled-dryings', async ({ request }) => {
      posted.push({ path: 'run', body: await request.json() });
      return HttpResponse.json({ id: 1 });
    }),
    http.post('/api/v1/drying-schedules', async ({ request }) => {
      posted.push({ path: 'rule', body: await request.json() });
      return HttpResponse.json({ id: 1 });
    }),
  );
  render(<PrintersPage />);
  return posted;
}

describe('PrintersPage — scheduling a drying', () => {
  it('After delay creates a one-shot run', async () => {
    const posted = mount();
    fireEvent.click(await screen.findByTitle('Start Drying'));
    fireEvent.click(await screen.findByRole('button', { name: 'After delay' }));
    fireEvent.click(screen.getByTestId('drying-start-confirm'));
    await waitFor(() => expect(posted.map((p) => p.path)).toEqual(['run']));
    expect(typeof (posted[0].body as { start_after: string }).start_after).toBe('string');
  });

  it('Repeat creates a rule', async () => {
    const posted = mount();
    fireEvent.click(await screen.findByTitle('Start Drying'));
    fireEvent.click(await screen.findByRole('button', { name: 'Repeat' }));
    fireEvent.click(screen.getByTestId('drying-start-confirm'));
    await waitFor(() => expect(posted.map((p) => p.path)).toEqual(['rule']));
    expect(posted[0].body).toMatchObject({ start_time: '01:00', weekdays: 127 });
  });

  it('Now still starts immediately', async () => {
    const posted = mount();
    fireEvent.click(await screen.findByTitle('Start Drying'));
    fireEvent.click(screen.getByTestId('drying-start-confirm'));
    await waitFor(() => expect(posted.map((p) => p.path)).toEqual(['now']));
  });

  it('names the actual blocker', async () => {
    mount({ dry_sf_reason: [3] });
    expect(await screen.findByTitle(/Retract the filament at the AMS outlet/)).toBeInTheDocument();
  });
});

describe('PrintersPage — scheduling while the unit cannot dry now', () => {
  it('a blocked unit still opens the popover; only "Now" is off', async () => {
    const posted = mount({ dry_sf_reason: [3] });
    fireEvent.click(await screen.findByTitle(/Retract the filament at the AMS outlet/));
    const popover = await screen.findByTestId('ams-drying-popover');
    expect(within(popover).getByRole('button', { name: 'Now' })).toBeDisabled();
    fireEvent.click(within(popover).getByTestId('drying-start-confirm'));
    await waitFor(() => expect(posted.map((p) => p.path)).toEqual(['run']));
    expect((posted[0].body as { start_after: string | null }).start_after).toBeNull();
  });

  it('a drying unit opens the popover to plan the next cycle', async () => {
    const posted = mount({ dry_time: 120 });
    fireEvent.click(await screen.findByTitle('Schedule the next drying'));
    const popover = await screen.findByTestId('ams-drying-popover');
    expect(within(popover).getByRole('button', { name: 'Now' })).toBeDisabled();
    fireEvent.click(within(popover).getByRole('button', { name: 'Repeat' }));
    fireEvent.click(within(popover).getByTestId('drying-start-confirm'));
    await waitFor(() => expect(posted.map((p) => p.path)).toEqual(['rule']));
  });

  it('the header says it schedules when it does', async () => {
    mount();
    fireEvent.click(await screen.findByTitle('Start Drying'));
    const popover = await screen.findByTestId('ams-drying-popover');
    fireEvent.click(within(popover).getByRole('button', { name: 'After delay' }));
    expect(within(popover).getByText('Schedule drying')).toBeInTheDocument();
  });
});
