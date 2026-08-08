/**
 * Bark provider in the notification modal (upstream Bambuddy #1495).
 *
 * Bark is the account-free iOS push app. It sits next to ntfy and Pushover for
 * one reason worth the extra provider: `critical` interruption level is
 * delivered through Silent mode and Focus, so a printer that stops at 03:00 can
 * actually wake somebody. That option therefore has to survive the round trip
 * from the select into `config` — a dropped value here is silent, and only ever
 * discovered on the night it was needed.
 */
import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { AddNotificationModal } from '../../components/AddNotificationModal';
import type { NotificationProvider } from '../../api/client';

function barkProvider(config: Record<string, unknown> = { device_key: 'abc123' }) {
  return {
    id: 1,
    name: 'My iPhone',
    provider_type: 'bark',
    enabled: true,
    config,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as unknown as NotificationProvider;
}

describe('AddNotificationModal — Bark', () => {
  it('offers Bark in the provider list and renders its fields', async () => {
    render(<AddNotificationModal provider={barkProvider()} onClose={() => undefined} />);

    await screen.findByDisplayValue('My iPhone');
    expect(screen.getByRole('option', { name: 'Bark' })).toBeInTheDocument();
    expect(screen.getByDisplayValue('abc123')).toBeInTheDocument();
    // The relay is the default, so the field is a placeholder rather than a value.
    expect(screen.getByPlaceholderText('https://api.day.app')).toBeInTheDocument();
    expect(screen.getByText(/interruption level/i)).toBeInTheDocument();
  });

  it('carries the critical level and group through to the saved config', async () => {
    let captured: { config: Record<string, unknown> } | null = null;
    server.use(
      http.patch('*/api/v1/notifications/1', async ({ request }) => {
        captured = (await request.json()) as { config: Record<string, unknown> };
        return HttpResponse.json({ id: 1 });
      }),
    );

    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={barkProvider()} onClose={onClose} />);

    await user.type(await screen.findByPlaceholderText('BamDude'), 'Printers');
    const levelRow = screen.getByText(/interruption level/i).closest('div')!;
    await user.selectOptions(within(levelRow).getByRole('combobox'), 'critical');

    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured!.config).toMatchObject({
      device_key: 'abc123',
      group: 'Printers',
      level: 'critical',
    });
  });

  it('refuses to save without a device key', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={barkProvider({})} onClose={onClose} />);

    await screen.findByDisplayValue('My iPhone');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // The modal stays open and says which field is missing — the request never
    // leaves, so the backend's own "Device key is required" is a second line of
    // defence rather than the only one.
    expect(onClose).not.toHaveBeenCalled();
  });
});
