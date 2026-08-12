import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { DeviceReportingModal } from '../../components/zigbee/DeviceReportingModal';
import { api } from '../../api/client';
import type { DeviceSettings } from '../../api/client';

const SENSOR: DeviceSettings = {
  ieee: 'aa:bb',
  kind: 'sensor',
  name: 'SONOFF SNZB-02DR2',
  adopted: true,
  editable: {
    temperature: ['min_interval', 'max_interval', 'reportable_change'],
    battery: ['min_interval', 'max_interval', 'reportable_change'],
  },
  units: { temperature: '°C', battery: '%' },
  desired: {
    temperature: { min_interval: 30, max_interval: 900, reportable_change: 0.1 },
    battery: { min_interval: 3600, max_interval: 10800, reportable_change: 0.5 },
  },
  applied: {
    temperature: {
      state: 'ok',
      verification: 'verified',
      values: { min_interval: 30, max_interval: 900, reportable_change: 0.1 },
      actual: null,
      at: '2026-08-03T10:00:00+00:00',
      describes_desired: true,
    },
    battery: {
      state: 'unanswered',
      verification: 'not-checked',
      values: { min_interval: 3600, max_interval: 10800, reportable_change: 0.5 },
      actual: null,
      at: '2026-08-03T10:00:00+00:00',
      describes_desired: true,
    },
  },
  poll_seconds: 30,
  poll_supported: false,
  stale_after_seconds: 21600,
};

const RELAY: DeviceSettings = {
  ...SENSOR,
  kind: 'plug',
  name: 'SONOFF S60ZBTPF',
  editable: { state: ['max_interval'] },
  units: { state: '' },
  desired: { state: { min_interval: 0, max_interval: 900, reportable_change: 1 } },
  applied: {
    state: {
      state: 'ok',
      verification: 'mismatch',
      values: { min_interval: 0, max_interval: 900, reportable_change: 1 },
      actual: { min_interval: 0, max_interval: 300, reportable_change: 1 },
      at: '2026-08-03T10:00:00+00:00',
      describes_desired: true,
    },
  },
  poll_supported: true,
};

describe('DeviceReportingModal', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('shows only the fields a target allows', async () => {
    // A relay has changed or it has not; there is no amount of change, and no
    // floor worth setting on a thing only we can flip.
    vi.spyOn(api, 'getDeviceSettings').mockResolvedValue(RELAY);

    render(<DeviceReportingModal ieee="aa:bb" deviceName="Plug" onClose={() => {}} />);

    expect(await screen.findByLabelText(/Longest silence/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Shortest gap/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Change by/i)).not.toBeInTheDocument();
  });

  it('says what the device stored instead', async () => {
    vi.spyOn(api, 'getDeviceSettings').mockResolvedValue(RELAY);

    render(<DeviceReportingModal ieee="aa:bb" deviceName="Plug" onClose={() => {}} />);

    expect(await screen.findByText(/stored something else/i)).toBeInTheDocument();
    expect(screen.getByText(/stored 300 s/i)).toBeInTheDocument();
  });

  it('explains a missing poll field instead of hiding it', async () => {
    vi.spyOn(api, 'getDeviceSettings').mockResolvedValue(SENSOR);

    render(<DeviceReportingModal ieee="aa:bb" deviceName="Sensor" onClose={() => {}} />);

    expect(await screen.findByText(/sleeps between reports/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Poll every/i)).not.toBeInTheDocument();
  });

  it('labels the change field in the target’s own unit', async () => {
    vi.spyOn(api, 'getDeviceSettings').mockResolvedValue(SENSOR);

    render(<DeviceReportingModal ieee="aa:bb" deviceName="Sensor" onClose={() => {}} />);

    expect(await screen.findByText('°C')).toBeInTheDocument();
  });

  it('saving a sleeping sensor is a success, not an error', async () => {
    vi.spyOn(api, 'getDeviceSettings').mockResolvedValue(SENSOR);
    vi.spyOn(api, 'updateDeviceSettings').mockResolvedValue({
      ...SENSOR,
      applied: {
        ...SENSOR.applied,
        temperature: { ...SENSOR.applied.temperature, state: 'unanswered', verification: 'not-checked' },
      },
    });

    render(<DeviceReportingModal ieee="aa:bb" deviceName="Sensor" onClose={() => {}} />);
    await screen.findByRole('button', { name: /farm defaults/i });
    await userEvent.click(screen.getByRole('button', { name: /save/i }));

    expect(await screen.findByText(/take effect when the device next wakes/i)).toBeInTheDocument();
  });

  it('shows the reason a refusal gives, not a generic failure', async () => {
    vi.spyOn(api, 'getDeviceSettings').mockResolvedValue(SENSOR);
    // A refusal the backend really gives, and one that does not collide with
    // the poll hint already on screen.
    vi.spyOn(api, 'updateDeviceSettings').mockRejectedValue(
      new Error("'temperature': the shortest gap between reports cannot be longer than the longest."),
    );

    render(<DeviceReportingModal ieee="aa:bb" deviceName="Sensor" onClose={() => {}} />);
    await screen.findByRole('button', { name: /farm defaults/i });
    await userEvent.click(screen.getByRole('button', { name: /save/i }));

    expect(await screen.findByText(/cannot be longer than the longest/i)).toBeInTheDocument();
  });

  it('returning to the farm defaults asks the backend to', async () => {
    vi.spyOn(api, 'getDeviceSettings').mockResolvedValue(SENSOR);
    const clear = vi.spyOn(api, 'clearDeviceSettings').mockResolvedValue(SENSOR);

    render(<DeviceReportingModal ieee="aa:bb" deviceName="Sensor" onClose={() => {}} />);
    await screen.findByRole('button', { name: /farm defaults/i });
    await userEvent.click(screen.getByRole('button', { name: /farm defaults/i }));

    await waitFor(() => expect(clear).toHaveBeenCalledWith('aa:bb'));
  });

  it('a device off the mesh gets an explanation instead of a form', async () => {
    vi.spyOn(api, 'getDeviceSettings').mockRejectedValue(new Error('This device is not on the network right now.'));

    render(<DeviceReportingModal ieee="aa:bb" deviceName="Sensor" onClose={() => {}} />);

    expect(await screen.findByText(/not on the network right now/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Longest silence/i)).not.toBeInTheDocument();
  });
});
