import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { SensorThresholdsModal } from '../../components/zigbee/SensorThresholdsModal';
import { api } from '../../api/client';
import type { ZigbeeSensor } from '../../api/client';

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
  measurements: { temperature: reading(23.4, '°C'), battery: reading(88, '%') },
};

describe('SensorThresholdsModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getSensorThresholds').mockResolvedValue({ thresholds: [] });
  });

  it('offers a row for every quantity the sensor measures, battery included', async () => {
    // Battery is excluded from the readings CHIPS because it describes the
    // device — but a flat cell is exactly what makes a sensor go silent, so it
    // is worth a limit.
    render(<SensorThresholdsModal isOpen onClose={() => {}} sensor={SENSOR} />);

    expect(await screen.findByLabelText(/temperature maximum/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/battery minimum/i)).toBeInTheDocument();
  });

  it('keeps a configured quantity the sensor is not currently reporting', async () => {
    // Without the union a sensor off the mesh shows an empty dialog and its own
    // settings become invisible and unremovable.
    vi.spyOn(api, 'getSensorThresholds').mockResolvedValue({
      thresholds: [
        { kind: 'humidity', min_value: null, max_value: 60, deadband: 2, enabled: true, state: 'ok', unit: '%' },
      ],
    });

    render(<SensorThresholdsModal isOpen onClose={() => {}} sensor={SENSOR} />);

    expect(await screen.findByLabelText(/humidity maximum/i)).toHaveValue(60);
  });

  it('sends only the rows that carry a limit', async () => {
    // A row with neither limit is refused by the backend; sending it would
    // turn an untouched row into an error message.
    const save = vi.spyOn(api, 'putSensorThresholds').mockResolvedValue({ thresholds: [] });

    render(<SensorThresholdsModal isOpen onClose={() => {}} sensor={SENSOR} />);
    await userEvent.type(await screen.findByLabelText(/temperature maximum/i), '30');
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(7, [
        { kind: 'temperature', min_value: null, max_value: 30, deadband: 0, enabled: true },
      ]),
    );
  });

  it('shows the reason the backend gives rather than guessing at one', async () => {
    vi.spyOn(api, 'putSensorThresholds').mockRejectedValue(
      new Error('A threshold needs a minimum, a maximum, or both.'),
    );

    render(<SensorThresholdsModal isOpen onClose={() => {}} sensor={SENSOR} />);
    await userEvent.type(await screen.findByLabelText(/temperature maximum/i), '30');
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(await screen.findByText(/needs a minimum/i)).toBeInTheDocument();
  });
});
