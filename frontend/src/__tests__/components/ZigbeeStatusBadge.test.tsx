/**
 * The badge that answers "why has this plug stopped responding".
 *
 * Its most important behaviour is the one where it renders nothing. An install
 * that has never wanted Zigbee must not grow a permanent grey indicator about a
 * feature it does not use, and a working radio needs no comment either — silence
 * is the good state.
 *
 * It exists because the `zigbee_status_changed` toast cannot cover the case that
 * matters most: that event fires on a *change*, so a dongle already unplugged
 * when BamDude started produces no event at all. Which is exactly when an
 * operator needs to be told.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';

import { render } from '../utils';
import { ZigbeeStatusBadge } from '../../components/zigbee/ZigbeeStatusBadge';
import { api } from '../../api/client';
import type { ZigbeeStatus } from '../../api/client';

function status(over: Partial<ZigbeeStatus> = {}): ZigbeeStatus {
  return { state: 'up', reason: null, coordinator: null, network: null, radio_changed: null, ...over };
}

describe('ZigbeeStatusBadge', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('renders nothing when Zigbee is disabled', async () => {
    vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue(status({ state: 'disabled' }));

    render(
      <div data-testid="host">
        <ZigbeeStatusBadge variant="dot" />
      </div>,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The shared render helper also mounts the toast portal into the container,
    // so the assertion has to be about our own host node, not the container.
    expect(screen.getByTestId('host')).toBeEmptyDOMElement();
  });

  it('renders nothing when the radio is up', async () => {
    vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue(status());

    render(
      <div data-testid="host">
        <ZigbeeStatusBadge variant="dot" />
      </div>,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The shared render helper also mounts the toast portal into the container,
    // so the assertion has to be about our own host node, not the container.
    expect(screen.getByTestId('host')).toBeEmptyDOMElement();
  });

  it('renders nothing while the radio is still starting', async () => {
    // Starting is transient and expected. Flagging it would train the operator
    // to ignore the badge.
    vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue(status({ state: 'starting' }));

    render(
      <div data-testid="host">
        <ZigbeeStatusBadge variant="dot" />
      </div>,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The shared render helper also mounts the toast portal into the container,
    // so the assertion has to be about our own host node, not the container.
    expect(screen.getByTestId('host')).toBeEmptyDOMElement();
  });

  it('shows the reason inline when the radio is down', async () => {
    vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue(
      status({ state: 'error', reason: 'Port busy - Zigbee2MQTT is the most likely owner' }),
    );

    render(<ZigbeeStatusBadge variant="inline" />);

    expect(await screen.findByText(/Zigbee2MQTT is the most likely owner/)).toBeInTheDocument();
  });

  it('carries the reason as a title on the dot, not as layout', async () => {
    vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue(status({ state: 'error', reason: 'no dongle' }));

    render(<ZigbeeStatusBadge variant="dot" />);

    expect(await screen.findByTitle(/no dongle/)).toBeInTheDocument();
  });

  it('falls back to a generic label when the backend gave no reason', async () => {
    vi.spyOn(api, 'getZigbeeStatus').mockResolvedValue(status({ state: 'error', reason: null }));

    render(<ZigbeeStatusBadge variant="inline" />);

    expect(await screen.findByText(/zigbee radio is down/i)).toBeInTheDocument();
  });
});
