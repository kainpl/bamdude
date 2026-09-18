/**
 * Event chips and event toggles on the REAL card, both fed by the API list.
 *
 * A separate file for the same reason `NotificationProviderSensorToggles`
 * exists: `NotificationProviderCard.test.tsx` mocks the component it is named
 * after, so assertions added there would be about the mock.
 *
 * What is pinned here: the card no longer keeps its own list. It used to write
 * out 34 toggles and 25 chips by hand — queue and sensor events had no chip at
 * all — and the dialog kept a third, shorter list. Everything now walks the
 * rows `GET /notifications/events` returns.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { NotificationProviderCard } from '../../components/NotificationProviderCard';
import { api } from '../../api/client';
import type { NotificationProvider } from '../../api/client';
import { PROVIDER_EVENTS } from '../fixtures/providerEvents';

function provider(over: Partial<NotificationProvider> = {}): NotificationProvider {
  return {
    id: 1,
    name: 'ntfy',
    provider_type: 'ntfy',
    enabled: true,
    config: { topic: 'bamdude' },
    quiet_hours_enabled: false,
    daily_digest_enabled: false,
    created_at: '2026-09-18T10:00:00Z',
    updated_at: '2026-09-18T10:00:00Z',
    ...over,
  } as NotificationProvider;
}

async function openEventSettings() {
  await userEvent.click(await screen.findByRole('button', { name: /Event Settings/i }));
}

describe('NotificationProviderCard — events from the API', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('offers every flag the API knows, not the subset the card used to list', async () => {
    render(<NotificationProviderCard provider={provider()} onEdit={() => {}} />);
    await openEventSettings();

    const toggles = await screen.findAllByRole('switch');
    // Plus the card's own non-event switches (enabled, quiet hours, digest).
    expect(toggles.length).toBeGreaterThanOrEqual(PROVIDER_EVENTS.length);
    expect(screen.getByRole('switch', { name: 'Job Failed' })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'AMS Humidity High' })).toBeInTheDocument();
  });

  it('saves a toggle under its own flag', async () => {
    const update = vi.spyOn(api, 'updateNotificationProvider').mockResolvedValue(provider());

    render(<NotificationProviderCard provider={provider()} onEdit={() => {}} />);
    await openEventSettings();
    await userEvent.click(await screen.findByRole('switch', { name: 'Job Failed' }));

    expect(update).toHaveBeenCalledWith(1, { on_queue_job_failed: true });
  });

  it('shows a chip for a subscribed event the old hand-written list had none for', async () => {
    render(
      <NotificationProviderCard
        provider={provider({ on_queue_job_failed: true } as Partial<NotificationProvider>)}
        onEdit={() => {}}
      />,
    );

    expect(await screen.findByText('Job Failed')).toBeInTheDocument();
  });

  it('shows no chip for an event the provider is not subscribed to', async () => {
    render(
      <NotificationProviderCard
        provider={provider({ on_queue_job_failed: false } as Partial<NotificationProvider>)}
        onEdit={() => {}}
      />,
    );

    // Wait for the event list, so "no chip" is an answer and not a race.
    await screen.findByRole('button', { name: /Event Settings/i });
    expect(screen.queryByText('Job Failed')).not.toBeInTheDocument();
  });

  it('keeps the progress duration floor beside its own event', async () => {
    render(
      <NotificationProviderCard
        provider={provider({ on_print_progress: true, progress_min_duration_minutes: 30 } as Partial<NotificationProvider>)}
        onEdit={() => {}}
      />,
    );
    await openEventSettings();

    expect(await screen.findByDisplayValue('30')).toBeInTheDocument();
  });
});
