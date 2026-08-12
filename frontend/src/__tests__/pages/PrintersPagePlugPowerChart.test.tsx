/**
 * The chart button on a printer's own plug row.
 *
 * Fixtures copied from PrintersPage.test.tsx — the plug row lives inside the
 * printer card, so the card has to render for the button to exist at all.
 */

import { describe, expect, it, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const PRINTER = {
  id: 1,
  name: 'A1 Mini',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'A1 Mini',
  enabled: true,
  nozzle_diameter: 0.4,
  nozzle_type: 'stainless_steel',
  location: null,
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const STATUS = {
  connected: true,
  state: 'IDLE',
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
};

const PLUG = {
  id: 4,
  name: 'A1Mini-101 Plug',
  plug_type: 'zigbee',
  enabled: true,
  printer_id: 1,
  controls_printer_power: true,
};

describe('the plug row on a printer card', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([PRINTER])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(STATUS)),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/by-printer/1', () => HttpResponse.json(PLUG)),
      http.get('/api/v1/smart-plugs/4/status', () =>
        HttpResponse.json({ state: 'ON', reachable: true, energy: { power: 72 } }),
      ),
      http.get('/api/v1/smart-plugs/4/power-history', () =>
        HttpResponse.json({
          points: [{ recorded_at: '2026-08-03T10:00:00+00:00', power: 72 }],
          bucket_seconds: 300,
          min_power: 3,
          avg_power: 66,
          max_power: 174,
        }),
      ),
    );
  });

  it('offers the power history where the plug already is', async () => {
    render(<PrintersPage />);

    const open = await screen.findByRole('button', { name: /power history/i });
    await userEvent.click(open);

    expect(await screen.findByText('174 W')).toBeInTheDocument();
  });
});
