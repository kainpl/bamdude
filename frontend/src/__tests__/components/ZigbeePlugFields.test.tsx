/**
 * The Zigbee device picker in the add/edit plug modal.
 *
 * There are no multiplier or divisor inputs here and there must not be: the
 * device reports its own scaling on the Metering cluster, so asking an operator
 * for it would invite a wrong answer where a right one is already available.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';

import { render } from '../utils';
import { ZigbeePlugFields } from '../../components/smartPlug/ZigbeePlugFields';
import { api } from '../../api/client';
import type { ZigbeeDevice } from '../../api/client';

const IEEE = 'a4:c1:38:0b:5a:9c:ff:ff';

function device(over: Partial<ZigbeeDevice> = {}): ZigbeeDevice {
  return {
    ieee: IEEE,
    nwk: 0xf6b4,
    name: null,
    kind: 'plug',
    adopted: false,
    measurements: [],
    manufacturer: 'SONOFF',
    model: 'S60ZBTPF',
    is_coordinator: false,
    is_plug: true,
    has_metering: true,
    has_electrical_measurement: true,
    ...over,
  };
}

function radioUp() {
  vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue({
    state: 'up',
    reason: null,
    coordinator: null,
    network: null,
    radio_changed: null,
  });
}

describe('ZigbeePlugFields', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('lists a paired plug', async () => {
    radioUp();
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [device()] });

    render(<ZigbeePlugFields value={null} onChange={() => {}} excludeIeees={[]} />);

    expect(await screen.findByRole('option', { name: /S60ZBTPF/ })).toBeInTheDocument();
  });

  it('omits a device already bound to another plug', async () => {
    radioUp();
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [device()] });

    render(<ZigbeePlugFields value={null} onChange={() => {}} excludeIeees={[IEEE]} />);

    expect(await screen.findByText(/no unbound zigbee/i)).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /S60ZBTPF/ })).not.toBeInTheDocument();
  });

  it('matches the exclusion case-insensitively', async () => {
    // zigpy stringifies EUI64 lower-case, but an operator may have pasted upper.
    radioUp();
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [device()] });

    render(<ZigbeePlugFields value={null} onChange={() => {}} excludeIeees={[IEEE.toUpperCase()]} />);

    expect(await screen.findByText(/no unbound zigbee/i)).toBeInTheDocument();
  });

  it('omits a device that is not a plug', async () => {
    radioUp();
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({
      devices: [device({ is_plug: false, model: 'SNZB-02' })],
    });

    render(<ZigbeePlugFields value={null} onChange={() => {}} excludeIeees={[]} />);

    await screen.findByText(/no unbound zigbee/i);
    expect(screen.queryByRole('option', { name: /SNZB-02/ })).not.toBeInTheDocument();
  });

  it('is disabled with an explanation when the coordinator is down', async () => {
    vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue({
      state: 'error',
      reason: 'no dongle',
      coordinator: null,
      network: null,
      radio_changed: null,
    });
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [] });

    render(<ZigbeePlugFields value={null} onChange={() => {}} excludeIeees={[]} />);

    expect(await screen.findByText(/coordinator is not connected/i)).toBeInTheDocument();
    expect(screen.getByRole('combobox')).toBeDisabled();
  });

  it('warns that a plug without metering will never report energy', async () => {
    radioUp();
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({
      devices: [device({ has_metering: false, has_electrical_measurement: false })],
    });

    render(<ZigbeePlugFields value={IEEE} onChange={() => {}} excludeIeees={[]} />);

    // The backend accepts such plugs deliberately, so the consumer has to be
    // told in advance rather than read an absent value as a zero.
    expect(await screen.findByText(/will stay empty/i)).toBeInTheDocument();
  });

  it('says nothing about energy for a plug that reports it', async () => {
    radioUp();
    vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [device()] });

    render(<ZigbeePlugFields value={IEEE} onChange={() => {}} excludeIeees={[]} />);

    await screen.findByRole('option', { name: /S60ZBTPF/ });
    expect(screen.queryByText(/will stay empty/i)).not.toBeInTheDocument();
  });
});
