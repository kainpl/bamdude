import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';

import { render } from '../utils';
import { PrinterConditions } from '../../components/zigbee/PrinterConditions';
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

function sensor(over: Partial<ZigbeeSensor> = {}): ZigbeeSensor {
  return {
    id: 1,
    name: 'Enclosure',
    location: null,
    printer_id: 7,
    printer_name: 'X1C',
    ieee: 'aa:bb',
    nwk: 1,
    manufacturer: 'SONOFF',
    model: 'SNZB-02DR2',
    power: 'battery',
    quirk_applied: true,
    unreachable: false,
    present: true,
    measurements: { temperature: reading(38.4, '°C') },
    ...over,
  };
}

describe('PrinterConditions', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('shows the reading of a sensor bound to this printer', async () => {
    vi.spyOn(api, 'getZigbeeSensors').mockResolvedValue({ sensors: [sensor()] });

    render(<PrinterConditions printerId={7} />);

    expect(await screen.findByText('38.4 °C')).toBeInTheDocument();
  });

  it('shows nothing for another printer\u2019s sensor', async () => {
    const sensors = vi.spyOn(api, 'getZigbeeSensors').mockResolvedValue({ sensors: [sensor({ printer_id: 8 })] });

    render(<PrinterConditions printerId={7} />);

    // Wait for the answer before claiming an absence: the first test in
    // this file renders a chip from these very fixtures, so what is
    // asserted below is the filter deciding, not a render that had not
    // happened.
    await waitFor(() => expect(sensors).toHaveBeenCalled());
    expect(screen.queryByText(/°C/)).not.toBeInTheDocument();
  });

  it('shows nothing for a sensor bound to a place', async () => {
    // ⚠️ Not a fallback to the printer's own location: that sensor already
    // appears on the location group's header, and repeating it here would say
    // the enclosure reads what the room reads.
    const sensors = vi.spyOn(api, 'getZigbeeSensors').mockResolvedValue({
      sensors: [
        sensor({
          printer_id: null,
          printer_name: null,
          location: { id: 1, name: 'Workshop', parent_id: null } as never,
        }),
      ],
    });

    render(<PrinterConditions printerId={7} />);

    await waitFor(() => expect(sensors).toHaveBeenCalled());
    expect(screen.queryByText(/°C/)).not.toBeInTheDocument();
  });

  it('keeps a sensor that is off the mesh, by name and with no numbers', async () => {
    vi.spyOn(api, 'getZigbeeSensors').mockResolvedValue({
      sensors: [sensor({ present: false, measurements: {} })],
    });

    render(<PrinterConditions printerId={7} />);

    expect(await screen.findByText('Enclosure')).toBeInTheDocument();
    expect(screen.queryByText(/°C/)).not.toBeInTheDocument();
  });

  it('renders nothing when the query fails', async () => {
    // A printer card must never break over a thermometer.
    const sensors = vi.spyOn(api, 'getZigbeeSensors').mockRejectedValue(new Error('radio down'));

    render(<PrinterConditions printerId={7} />);

    await waitFor(() => expect(sensors).toHaveBeenCalled());
    expect(screen.queryByText(/°C/)).not.toBeInTheDocument();
    expect(screen.queryByText('Enclosure')).not.toBeInTheDocument();
  });
});
