import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { SensorCard } from '../../components/zigbee/SensorCard';
import type { SensorMeasurement, ZigbeeSensor } from '../../api/client';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) => (vars ? `${key}:${JSON.stringify(vars)}` : key),
  }),
}));

function measurement(over: Partial<SensorMeasurement> = {}): SensorMeasurement {
  return {
    value: 30.2,
    unit: '°C',
    last_report_at: new Date().toISOString(),
    stale: false,
    reporting: 'ok',
    verification: 'verified',
    ...over,
  };
}

function sensor(over: Partial<ZigbeeSensor> = {}): ZigbeeSensor {
  return {
    id: 1,
    name: 'Майстерня',
    location: { id: 2, name: 'Shop 2', parent_id: null, path: 'Shop 2' },
    ieee: 'aa:bb:cc:dd:ee:ff:00:11',
    nwk: 123,
    manufacturer: 'SONOFF',
    model: 'SNZB-02DR2',
    power: 'battery',
    quirk_applied: true,
    unreachable: false,
    present: true,
    measurements: { temperature: measurement() },
    ...over,
  };
}

function renderCard(over: Partial<ZigbeeSensor> = {}) {
  return render(<SensorCard sensor={sensor(over)} onEdit={() => {}} onUnbind={() => {}} canEdit canDelete />);
}

describe('SensorCard', () => {
  it('shows the reading with its unit', () => {
    renderCard();

    expect(screen.getByText(/30\.2 °C/)).toBeInTheDocument();
  });

  it('draws a missing value as a dash, never as zero', () => {
    // A fabricated reading is worse than a missing one: a number of the right
    // shape gets believed.
    renderCard({ measurements: { temperature: measurement({ value: null, last_report_at: null }) } });

    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.queryByText(/\b0\b/)).not.toBeInTheDocument();
  });

  it('says a device off the mesh is off the mesh, and shows no readings', () => {
    renderCard({ present: false, unreachable: true, power: null, measurements: {} });

    // Exact keys, not a substring: `notOnNetworkHint` contains `notOnNetwork`,
    // and a loose matcher would pass on either of them alone.
    expect(screen.getByText('settings.zigbee.sensors.notOnNetworkHint')).toBeInTheDocument();
    expect(screen.queryByText(/measurement\.temperature/)).not.toBeInTheDocument();
  });

  it('keeps "not answering" apart from "not on the network"', () => {
    // Different subjects: one is the device being absent, the other is a
    // device that is present and silent.
    renderCard({ unreachable: true });

    expect(screen.getByText('settings.zigbee.sensors.notAnswering')).toBeInTheDocument();
    expect(screen.queryByText('settings.zigbee.sensors.notOnNetworkHint')).not.toBeInTheDocument();
  });

  it('puts the battery in the header, not among the readings', () => {
    renderCard({
      measurements: { temperature: measurement(), battery: measurement({ value: 87, unit: '%' }) },
    });

    expect(screen.getByText(/sensors\.battery/)).toBeInTheDocument();
    expect(screen.queryByText(/measurement\.battery/)).not.toBeInTheDocument();
  });

  it('shows mains power instead of a battery when the device has none', () => {
    renderCard({ power: 'mains', measurements: { temperature: measurement() } });

    expect(screen.getByText(/mainsPowered/)).toBeInTheDocument();
  });

  it('hides the controls a viewer may not use', () => {
    render(<SensorCard sensor={sensor()} onEdit={() => {}} onUnbind={() => {}} canEdit={false} canDelete={false} />);

    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
