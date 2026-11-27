/**
 * Notify! on the REAL card (`NotificationProviderCard.test.tsx` mocks the component).
 *
 * Browser (`WB…`) and group (`GRP…`) recipients take text only, so the photo
 * switch is locked and says why, as upstream's card does.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { NotificationProviderCard } from '../../components/NotificationProviderCard';
import type { NotificationProvider } from '../../api/client';

const HINT = 'Browser and group recipients support text notifications only; photo attachments are unavailable.';

function provider(deviceId: string, over: Partial<NotificationProvider> = {}): NotificationProvider {
  return {
    id: 1,
    name: 'Notify',
    provider_type: 'notify',
    enabled: true,
    config: { device_id: deviceId },
    quiet_hours_enabled: false,
    attach_photo: true,
    daily_digest_enabled: false,
    created_at: '2026-11-27T10:00:00Z',
    updated_at: '2026-11-27T10:00:00Z',
    ...over,
  } as NotificationProvider;
}

async function openSettings() {
  await userEvent.click(await screen.findByRole('button', { name: /Event Settings/i }));
}

describe('NotificationProviderCard — Notify! photo switch', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('explains why the photo switch is locked for a browser recipient', async () => {
    render(<NotificationProviderCard provider={provider('WB12345678901234')} onEdit={() => {}} />);
    await openSettings();

    expect(await screen.findByText(HINT)).toBeInTheDocument();
  });

  it('shows no such hint for an iOS device', async () => {
    render(<NotificationProviderCard provider={provider('IO12345678901234')} onEdit={() => {}} />);
    await openSettings();

    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
  });
});
