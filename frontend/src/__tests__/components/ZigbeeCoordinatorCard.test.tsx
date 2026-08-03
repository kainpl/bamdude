/**
 * The Zigbee coordinator card.
 *
 * `reason` is rendered verbatim on purpose: it is the whole explanation of why
 * the radio is not up ("port busy - Zigbee2MQTT or Home Assistant is the most
 * likely owner"), and mapping it to a friendlier string throws away the only
 * thing that tells an operator what to do.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { ZigbeeCoordinatorCard } from '../../components/zigbee/ZigbeeCoordinatorCard';
import { api } from '../../api/client';
import type { ZigbeeDevice, ZigbeeStatus } from '../../api/client';

const DISABLED: ZigbeeStatus = { state: 'disabled', reason: null, coordinator: null, network: null };
const UP: ZigbeeStatus = { state: 'up', reason: null, coordinator: null, network: null };

function device(overrides: Partial<ZigbeeDevice> = {}): ZigbeeDevice {
  return {
    ieee: 'aa:bb:cc:dd:ee:ff:00:11',
    nwk: 123,
    manufacturer: 'SONOFF',
    model: 'S60ZBTPF',
    kind: 'plug',
    measurements: [],
    name: null,
    adopted: false,
    is_coordinator: false,
    is_plug: true,
    has_metering: true,
    has_electrical_measurement: true,
    ...overrides,
  };
}

function stub(status: ZigbeeStatus) {
  vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue(status);
  vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({ devices: [] });
  vi.spyOn(api, 'getSmartPlugs').mockResolvedValue([]);
  vi.spyOn(api, 'getSettings').mockResolvedValue({
    zigbee_enabled: status.state !== 'disabled',
    zigbee_transport: 'ethernet',
    zigbee_path: '192.168.1.50:6638',
  } as never);
}

describe('ZigbeeCoordinatorCard', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('shows the failure reason verbatim', async () => {
    stub({
      state: 'error',
      reason: 'Port busy - Zigbee2MQTT is the most likely owner',
      coordinator: null,
      network: null,
    });

    render(<ZigbeeCoordinatorCard />);

    expect(await screen.findByText(/Zigbee2MQTT is the most likely owner/)).toBeInTheDocument();
  });

  it('shows the radio identity and channel when up', async () => {
    stub({
      state: 'up',
      reason: null,
      coordinator: {
        ieee: '34:8d:13:ff:fe:11:e4:6f',
        nwk: 0,
        model: 'EZSP',
        manufacturer: 'Silicon Labs',
        version: '8.0.2',
      },
      network: { channel: 15, pan_id: 6754 },
    });

    render(<ZigbeeCoordinatorCard />);

    expect(await screen.findByText(/34:8d:13:ff:fe:11:e4:6f/)).toBeInTheDocument();
    expect(screen.getByText(/15/)).toBeInTheDocument();
  });

  it('does not enumerate serial ports on the ethernet transport', async () => {
    stub(DISABLED);
    const ports = vi.spyOn(api, 'getZigbeePorts').mockResolvedValue({ ports: [] });

    render(<ZigbeeCoordinatorCard />);
    await screen.findByRole('button', { name: /connect/i });

    expect(ports).not.toHaveBeenCalled();
  });

  it('saves the settings before restarting, in that order', async () => {
    stub(DISABLED);
    const calls: string[] = [];
    vi.spyOn(api, 'updateSettings').mockImplementation(async () => {
      calls.push('save');
      return {} as never;
    });
    vi.spyOn(api, 'restartZigbeeCoordinator').mockImplementation(async () => {
      calls.push('restart');
      return { state: 'up', reason: null, coordinator: null, network: null };
    });

    render(<ZigbeeCoordinatorCard />);
    await userEvent.click(await screen.findByRole('button', { name: /connect/i }));

    // Restarting against unsaved settings would connect to the old path and look
    // like the new one did not work.
    await waitFor(() => expect(calls).toEqual(['save', 'restart']));
  });

  it('an empty port list is not an error', async () => {
    stub({ ...DISABLED });
    vi.spyOn(api, 'getSettings').mockResolvedValue({
      zigbee_enabled: false,
      zigbee_transport: 'usb',
      zigbee_path: '',
    } as never);
    vi.spyOn(api, 'getZigbeePorts').mockResolvedValue({ ports: [] });

    render(<ZigbeeCoordinatorCard />);

    // findBy, not getBy: the transport comes from the settings query, so the
    // USB-only part of the form appears a tick after the first render.
    expect(await screen.findByText(/no serial ports/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /connect/i })).toBeEnabled();
  });

  it('pairing is refused while the radio is not up', async () => {
    stub({ state: 'error', reason: 'no dongle', coordinator: null, network: null });

    render(<ZigbeeCoordinatorCard />);

    expect(await screen.findByRole('button', { name: /pair a device/i })).toBeDisabled();
  });

  describe('the paired list describes each device in its own terms', () => {
    it('a sensor is named by what it measures, not by a relay vocabulary', async () => {
      // It carries neither metering cluster, so the plug wording called it
      // "switching only" -- a lesser relay rather than a different device.
      stub(UP);
      vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({
        devices: [
          device({
            kind: 'sensor',
            model: 'SNZB-02DR2',
            measurements: ['temperature', 'humidity'],
            is_plug: false,
            has_metering: false,
            has_electrical_measurement: false,
          }),
        ],
      });

      render(<ZigbeeCoordinatorCard />);

      expect(await screen.findByText(/measures temperature, humidity/i)).toBeInTheDocument();
      expect(screen.queryByText(/switching only/i)).not.toBeInTheDocument();
    });

    it('an adopted sensor does not read as free', async () => {
      // "In use" was computed by searching the plug list, which knows nothing
      // of sensors -- so every adopted one looked available to take.
      stub(UP);
      vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({
        devices: [device({ kind: 'sensor', measurements: ['temperature'], is_plug: false, adopted: true })],
      });

      render(<ZigbeeCoordinatorCard />);

      expect(await screen.findByText(/already added/i)).toBeInTheDocument();
    });

    it('the hardware name wins over the model when it is known', async () => {
      stub(UP);
      vi.spyOn(api, 'getZigbeeDevices').mockResolvedValue({
        devices: [device({ name: 'SONOFF SNZB-02DR2', model: 'SNZB-02DR2' })],
      });

      render(<ZigbeeCoordinatorCard />);

      expect(await screen.findByText(/SONOFF SNZB-02DR2/)).toBeInTheDocument();
    });
  });
});
