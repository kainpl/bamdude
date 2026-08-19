import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { LocationConditions } from '../../components/zigbee/LocationConditions';
import { api } from '../../api/client';
import type { ZigbeeSensor } from '../../api/client';

vi.mock('recharts', () => ({
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>,
  Line: () => <div data-testid="line" />,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Legend: () => null,
}));

const LOCATIONS = {
  locations: [
    {
      id: 1,
      name: 'Workshop',
      parent_id: null,
      path: 'Workshop',
      depth: 1,
      printer_count: 1,
      sensor_count: 1,
      queued_count: 0,
    },
    {
      id: 2,
      name: 'Shelf 1',
      parent_id: 1,
      path: 'Workshop / Shelf 1',
      depth: 2,
      printer_count: 1,
      sensor_count: 1,
      queued_count: 0,
    },
  ],
};

function reading(value: number, unit: string, over: Record<string, unknown> = {}) {
  return {
    value,
    unit,
    last_report_at: '2026-08-04T10:00:00+00:00',
    stale: false,
    reporting: 'ok',
    verification: 'verified',
    ...over,
  };
}

function sensor(over: Partial<ZigbeeSensor> = {}): ZigbeeSensor {
  return {
    id: 1,
    name: 'Workshop sensor',
    location: { id: 1, name: 'Workshop', parent_id: null, path: 'Workshop' },
    printer_id: null,
    printer_name: null,
    ieee: 'aa:bb',
    nwk: 1,
    manufacturer: 'SONOFF',
    model: 'SNZB-02DR2',
    power: 'battery',
    quirk_applied: true,
    unreachable: false,
    present: true,
    measurements: { temperature: reading(23.400000000000002, '°C'), humidity: reading(41, '%') },
    ...over,
  };
}

function stub(sensors: ZigbeeSensor[]) {
  vi.spyOn(api, 'getZigbeeSensors').mockResolvedValue({ sensors });
  vi.spyOn(api, 'getPrinterLocations').mockResolvedValue(LOCATIONS);
}

describe('LocationConditions', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('shows the value with its unit', async () => {
    // A number with no unit is not a reading. This is the assertion an
    // "element renders" test would have passed without.
    stub([sensor()]);

    render(<LocationConditions locationId={1} />);

    expect(await screen.findByText('23.4 °C')).toBeInTheDocument();
    expect(screen.getByText('41 %')).toBeInTheDocument();
  });

  it('shows an ancestor sensor in a descendant group', async () => {
    stub([sensor()]);

    render(<LocationConditions locationId={2} />);

    expect(await screen.findByText('23.4 °C')).toBeInTheDocument();
  });

  it('gives a sensor off the mesh its name and no numbers', async () => {
    // Its measurements are empty because it is absent, not because the room has
    // no temperature. A dead sensor and no sensor must not look alike.
    stub([sensor({ present: false, measurements: {} })]);

    render(<LocationConditions locationId={1} />);

    expect(await screen.findByText('Workshop sensor')).toBeInTheDocument();
    expect(screen.queryByText(/°C/)).not.toBeInTheDocument();
  });

  it('opens the chart for the sensor whose chip was clicked', async () => {
    // With two chips the wrong one is a plausible bug and an invisible one:
    // both open a chart, and the chart looks right.
    const near = sensor({
      id: 11,
      name: 'Shelf sensor',
      location: { id: 2, name: 'Shelf 1', parent_id: 1, path: 'Workshop / Shelf 1' },
    });
    stub([sensor({ id: 10, name: 'Workshop sensor' }), near]);
    const history = vi.spyOn(api, 'getSensorHistory').mockResolvedValue({
      points: [],
      bucket_seconds: 300,
      min_value: null,
      avg_value: null,
      max_value: null,
    });
    vi.spyOn(api, 'getSettings').mockResolvedValue({ time_format: 'system' } as never);

    render(<LocationConditions locationId={2} />);

    await userEvent.click(await screen.findByRole('button', { name: /Workshop sensor/ }));

    await waitFor(() => expect(history).toHaveBeenCalledWith(10, 'temperature', 24));
  });

  it('renders nothing without the sensor permission', async () => {
    // The locations card inherited the wrong permission and hid itself from the
    // people it was for. Gating is a claim that has to be tested.
    server.use(
      http.get('/api/v1/auth/me', () =>
        HttpResponse.json({
          id: 2,
          username: 'viewer',
          role: 'user',
          is_active: true,
          is_admin: false,
          groups: [{ id: 2, name: 'NoSensors' }],
          permissions: ['printers:read'],
          created_at: '2024-01-01T00:00:00Z',
        }),
      ),
    );
    stub([sensor()]);
    const me = vi.spyOn(api, 'getCurrentUser');

    render(<LocationConditions locationId={1} />);

    // Wait for the user to have loaded before claiming an absence. The first
    // test in this file renders a chip from these very fixtures, so what is
    // asserted below is the gate deciding, not a render that had not happened.
    await waitFor(() => expect(me).toHaveBeenCalled());
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByText('23.4 °C')).not.toBeInTheDocument();
    expect(api.getZigbeeSensors).not.toHaveBeenCalled();
  });

  it('renders nothing for the ungrouped group', async () => {
    stub([sensor()]);
    const me = vi.spyOn(api, 'getCurrentUser');

    render(<LocationConditions locationId={null} />);

    await waitFor(() => expect(me).toHaveBeenCalled());
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByText('23.4 °C')).not.toBeInTheDocument();
    expect(api.getZigbeeSensors).not.toHaveBeenCalled();
  });
});
