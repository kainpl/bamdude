import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { SensorsSection } from '../../components/zigbee/SensorsSection';
import { api } from '../../api/client';
import type { ZigbeeSensor, ZigbeeStatus } from '../../api/client';

const UP: ZigbeeStatus = { state: 'up', reason: null, coordinator: null, network: null };
const DOWN: ZigbeeStatus = { state: 'error', reason: 'no dongle', coordinator: null, network: null };

function sensor(over: Partial<ZigbeeSensor> = {}): ZigbeeSensor {
  return {
    id: 1,
    name: 'Майстерня',
    location: null,
    ieee: 'aa:bb',
    nwk: 1,
    manufacturer: 'SONOFF',
    model: 'SNZB-02DR2',
    power: 'battery',
    quirk_applied: true,
    unreachable: false,
    present: true,
    measurements: {},
    ...over,
  };
}

function stub(status: ZigbeeStatus, sensors: ZigbeeSensor[]) {
  vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue(status);
  vi.spyOn(api, 'getZigbeeSensors').mockResolvedValue({ sensors });
  vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [] });
  vi.spyOn(api, 'getPrinterLocations').mockResolvedValue({ locations: [] });
}

describe('SensorsSection', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('lists what has been added', async () => {
    stub(UP, [sensor()]);

    render(<SensorsSection adoptDevice={null} onAdoptHandled={() => {}} />);

    expect(await screen.findByText('Майстерня')).toBeInTheDocument();
  });

  it('says nothing is added yet rather than looking broken', async () => {
    stub(UP, []);

    render(<SensorsSection adoptDevice={null} onAdoptHandled={() => {}} />);

    expect(await screen.findByText(/No sensors added yet/i)).toBeInTheDocument();
  });

  it('explains a downed radio once, above the list, not on every card', async () => {
    stub(DOWN, [sensor({ present: false }), sensor({ id: 2, name: 'Склад', present: false })]);

    render(<SensorsSection adoptDevice={null} onAdoptHandled={() => {}} />);

    // The cards arrive on their own query, which settles after the status one
    // the banner reads -- so wait for a card, not for the banner.
    expect(await screen.findByText('Майстерня')).toBeInTheDocument();
    expect(screen.getByText('Склад')).toBeInTheDocument();
    expect(screen.getByText(/radio is down/i)).toBeInTheDocument();
  });

  it('the unbind confirmation names the boundary it does not cross', async () => {
    // Unbinding is not removing from the network. The confirmation is the only
    // place a person learns the difference.
    stub(UP, [sensor()]);

    render(<SensorsSection adoptDevice={null} onAdoptHandled={() => {}} />);
    await screen.findByText('Майстерня');
    await userEvent.click(screen.getByLabelText(/delete/i));

    expect(await screen.findByText(/stays on the Zigbee network/i)).toBeInTheDocument();
    expect(screen.getByText(/separate action/i)).toBeInTheDocument();
  });
});
