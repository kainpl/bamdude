import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { SensorHistoryModal } from '../../components/zigbee/SensorHistoryModal';
import { api } from '../../api/client';
import type { SensorHistory, ZigbeeSensor } from '../../api/client';

// jsdom gives ResponsiveContainer no size, so a real chart renders nothing.
// The Y domain is captured because it is the one thing that must NOT be copied
// from HeaterHistoryModal.
vi.mock('recharts', () => ({
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>,
  Line: () => <div data-testid="line" />,
  XAxis: () => null,
  YAxis: (props: { domain?: unknown }) => <div data-testid="y-axis" data-domain={JSON.stringify(props.domain)} />,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Legend: () => null,
}));

function reading(value: number, unit: string) {
  return {
    value,
    unit,
    last_report_at: '2026-08-04T10:00:00+00:00',
    stale: false,
    reporting: 'ok',
    verification: 'verified',
  };
}

const SENSOR: ZigbeeSensor = {
  id: 7,
  name: 'Майстерня',
  location: { id: 1, name: 'Workshop', parent_id: null, path: 'Workshop' },
  ieee: 'aa:bb',
  nwk: 1,
  manufacturer: 'SONOFF',
  model: 'SNZB-02DR2',
  power: 'battery',
  quirk_applied: true,
  unreachable: false,
  present: true,
  measurements: {
    temperature: reading(23.4, '°C'),
    humidity: reading(41, '%'),
    battery: reading(88, '%'),
  },
};

const HISTORY: SensorHistory = {
  points: [
    { recorded_at: '2026-08-04T10:00:00+00:00', value: 23.4 },
    { recorded_at: '2026-08-04T10:05:00+00:00', value: 23.9 },
  ],
  bucket_seconds: 300,
  min_value: 21.4,
  avg_value: 23.1,
  max_value: 25.8,
};

describe('SensorHistoryModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getSensorHistory').mockResolvedValue(HISTORY);
    vi.spyOn(api, 'getSettings').mockResolvedValue({ time_format: 'system' } as never);
  });

  it('opens on the first quantity the sensor measures', async () => {
    render(<SensorHistoryModal isOpen onClose={() => {}} sensor={SENSOR} />);

    await waitFor(() => expect(api.getSensorHistory).toHaveBeenCalledWith(7, 'temperature', 24));
  });

  it('offers only what this sensor measures, and not its battery', async () => {
    // Battery describes the device. A "battery" tab in a chart of room
    // conditions is a different subject on the same axis.
    render(<SensorHistoryModal isOpen onClose={() => {}} sensor={SENSOR} />);
    await screen.findByTestId('line-chart');

    expect(screen.getByRole('button', { name: 'humidity' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'battery' })).not.toBeInTheDocument();
  });

  it('asks for the quantity that was picked', async () => {
    render(<SensorHistoryModal isOpen onClose={() => {}} sensor={SENSOR} />);
    await screen.findByTestId('line-chart');

    await userEvent.click(screen.getByRole('button', { name: 'humidity' }));

    await waitFor(() => expect(api.getSensorHistory).toHaveBeenCalledWith(7, 'humidity', 24));
  });

  it('asks for the window that was picked', async () => {
    render(<SensorHistoryModal isOpen onClose={() => {}} sensor={SENSOR} />);
    await screen.findByTestId('line-chart');

    await userEvent.click(screen.getByRole('button', { name: '7d' }));

    await waitFor(() => expect(api.getSensorHistory).toHaveBeenCalledWith(7, 'temperature', 168));
  });

  it('does not start the scale at zero', async () => {
    // HeaterHistoryModal uses [0, 'auto'] because zero means "off" for a nozzle.
    // A room sits between 21 and 26 °C, and on a scale from zero that is a flat
    // line with no information in it.
    render(<SensorHistoryModal isOpen onClose={() => {}} sensor={SENSOR} />);

    const axis = await screen.findByTestId('y-axis');
    expect(JSON.parse(axis.getAttribute('data-domain')!)).toEqual(['auto', 'auto']);
  });

  it('says so when nothing has been recorded', async () => {
    vi.spyOn(api, 'getSensorHistory').mockResolvedValue({ ...HISTORY, points: [] });

    render(<SensorHistoryModal isOpen onClose={() => {}} sensor={SENSOR} />);

    expect(await screen.findByText(/Nothing recorded yet/i)).toBeInTheDocument();
  });
});
