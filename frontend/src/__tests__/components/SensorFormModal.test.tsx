import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { SensorFormModal } from '../../components/zigbee/SensorFormModal';
import { api } from '../../api/client';
import type { ZigbeeDevice, ZigbeeSensor } from '../../api/client';

function device(over: Partial<ZigbeeDevice> = {}): ZigbeeDevice {
  return {
    ieee: 'aa:bb:cc:dd:ee:ff:00:11',
    nwk: 1,
    manufacturer: 'SONOFF',
    model: 'SNZB-02DR2',
    kind: 'sensor',
    measurements: ['temperature', 'humidity'],
    name: 'SONOFF SNZB-02DR2',
    adopted: false,
    is_coordinator: false,
    is_plug: false,
    has_metering: false,
    has_electrical_measurement: false,
    ...over,
  };
}

function existing(over: Partial<ZigbeeSensor> = {}): ZigbeeSensor {
  return {
    id: 7,
    name: 'Майстерня',
    location: null,
    ieee: 'aa:bb',
    nwk: 1,
    manufacturer: null,
    model: null,
    power: 'battery',
    quirk_applied: null,
    unreachable: false,
    present: true,
    measurements: {},
    ...over,
  };
}

describe('SensorFormModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getPrinterLocations').mockResolvedValue({ locations: [] });
  });

  it('offers only paired sensors nobody has added', async () => {
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({
      devices: [
        device(),
        device({ ieee: 'ff:ff', name: 'taken', adopted: true }),
        device({ ieee: '11:11', kind: 'plug', is_plug: true, name: 'a plug' }),
      ],
    });

    render(<SensorFormModal sensor={null} initialDevice={null} onClose={() => {}} />);

    expect(await screen.findByRole('option', { name: /SNZB-02DR2/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /taken/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /a plug/ })).not.toBeInTheDocument();
  });

  it('starts from the hardware name so the operator renames rather than types', async () => {
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [device()] });

    render(<SensorFormModal sensor={null} initialDevice={device()} onClose={() => {}} />);

    expect(await screen.findByDisplayValue('SONOFF SNZB-02DR2')).toBeInTheDocument();
  });

  it('adopts with the device, the name and the place', async () => {
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [device()] });
    const adopt = vi.spyOn(api, 'adoptZigbeeSensor').mockResolvedValue({ id: 1, name: 'Bench' });

    render(<SensorFormModal sensor={null} initialDevice={device()} onClose={() => {}} />);
    const name = await screen.findByDisplayValue('SONOFF SNZB-02DR2');
    await userEvent.clear(name);
    await userEvent.type(name, 'Bench');
    await userEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() =>
      expect(adopt).toHaveBeenCalledWith({
        zigbee_ieee: 'aa:bb:cc:dd:ee:ff:00:11',
        name: 'Bench',
        location_id: null,
      }),
    );
  });

  it('editing does not offer to change the device', async () => {
    // A sensor's device does not change: to move to another one you unbind and
    // adopt again.
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [device()] });

    render(<SensorFormModal sensor={existing()} initialDevice={null} onClose={() => {}} />);

    expect(await screen.findByDisplayValue('Майстерня')).toBeInTheDocument();
    expect(screen.queryByLabelText(/device/i)).not.toBeInTheDocument();
  });
});
