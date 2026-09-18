/**
 * Provider-specific config in the notification modal.
 *
 * Every provider's settings live in one untyped `config` blob, so what the modal
 * puts in it is the whole contract — there is no schema to catch a dropped or
 * malformed field. These tests pin the two cases where getting it wrong is
 * silent until the moment the notification was needed.
 */
import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { AddNotificationModal } from '../../components/AddNotificationModal';
import type { NotificationProvider } from '../../api/client';

function buildProvider(provider_type: string, name: string, config: Record<string, unknown>) {
  return {
    id: 1,
    name,
    provider_type,
    enabled: true,
    config,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as unknown as NotificationProvider;
}

const barkProvider = (config: Record<string, unknown> = { device_key: 'abc123' }) =>
  buildProvider('bark', 'My iPhone', config);

const haProvider = (config: Record<string, unknown>) => buildProvider('homeassistant', 'My HA', config);

/**
 * Bark is the account-free iOS push app. It sits next to ntfy and Pushover for
 * one reason worth the extra provider: `critical` interruption level is
 * delivered through Silent mode and Focus, so a printer that stops at 03:00 can
 * actually wake somebody. That option therefore has to survive the round trip
 * from the select into `config` (upstream Bambuddy #1495).
 */
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

/**
 * Home Assistant custom service data (upstream Bambuddy #1441). A notify service
 * takes its mobile-push options — priority, ttl, channel — under a nested
 * `data` object. It is JSON rather than key=value lines precisely so `ttl: 0`
 * stays the number HA's Android integration wants.
 */
describe('AddNotificationModal — Home Assistant custom data', () => {
  it('renders the Data field for Home Assistant', async () => {
    render(<AddNotificationModal provider={haProvider({ service: 'notify.mobile_app_x' })} onClose={() => undefined} />);

    await screen.findByDisplayValue('My HA');
    expect(screen.getByPlaceholderText(/"priority": "high"/)).toBeInTheDocument();
  });

  it('saves a valid JSON object as typed', async () => {
    let captured: { config: Record<string, unknown> } | null = null;
    server.use(
      http.patch('*/api/v1/notifications/1', async ({ request }) => {
        captured = (await request.json()) as { config: Record<string, unknown> };
        return HttpResponse.json({ id: 1 });
      }),
    );

    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <AddNotificationModal
        provider={haProvider({ service: 'notify.mobile_app_x', data: '{"ttl": 0}' })}
        onClose={onClose}
      />,
    );

    await screen.findByDisplayValue('My HA');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    // Stored as the text the user typed; the sender parses it. Keeping it a
    // string is what lets the field round-trip back into the textarea unchanged.
    expect(captured!.config).toMatchObject({ data: '{"ttl": 0}' });
  });

  it('refuses malformed JSON before it can be saved', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={haProvider({ data: '{not json' })} onClose={onClose} />);

    await screen.findByDisplayValue('My HA');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText(/valid JSON object/i)).toBeInTheDocument();
  });

  it('refuses valid JSON of the wrong shape', async () => {
    // `["a"]` parses fine and would be posted straight through to HA, which
    // wants an object — the check is on the shape, not just on parseability.
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={haProvider({ data: '["a"]' })} onClose={onClose} />);

    await screen.findByDisplayValue('My HA');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText(/valid JSON object/i)).toBeInTheDocument();
  });
});

/**
 * Signal, via a self-hosted signal-cli-rest-api instance. Its recipients
 * don't fit the generic scalar-field config: signal-cli-rest-api can't mix
 * individual numbers and a group ID in one request, so the form is a type
 * switch between a dynamic list of numbers and a single group field, and
 * only the active shape is required to be non-empty.
 */
describe('AddNotificationModal — Signal', () => {
  const signalProvider = (config: Record<string, unknown>) => buildProvider('signal', 'My Signal', config);

  it('offers Signal in the provider list and renders its fields', async () => {
    render(
      <AddNotificationModal
        provider={signalProvider({
          server: 'http://localhost:8080',
          sender_number: '+15550000000',
          recipient_type: 'numbers',
          numbers: '+15551111111',
        })}
        onClose={() => undefined}
      />,
    );

    await screen.findByDisplayValue('My Signal');
    expect(screen.getByRole('option', { name: 'Signal CLI API' })).toBeInTheDocument();
    expect(screen.getByDisplayValue('http://localhost:8080')).toBeInTheDocument();
    expect(screen.getByDisplayValue('+15550000000')).toBeInTheDocument();
    expect(screen.getByDisplayValue('+15551111111')).toBeInTheDocument();
  });

  it('switches from a numbers list to a single Group ID field', async () => {
    const user = userEvent.setup();
    render(
      <AddNotificationModal
        provider={signalProvider({
          server: 'http://localhost:8080',
          sender_number: '+15550000000',
          recipient_type: 'numbers',
          numbers: '+15551111111',
        })}
        onClose={() => undefined}
      />,
    );

    await screen.findByDisplayValue('My Signal');
    const recipientTypeRow = screen.getByText('Recipient Type').closest('div')!;
    await user.selectOptions(within(recipientTypeRow).getByRole('combobox'), 'group');

    expect(screen.queryByDisplayValue('+15551111111')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('group.XXXXXXXX==')).toBeInTheDocument();
  });

  it('adds a number row and saves the joined list', async () => {
    let captured: { config: Record<string, unknown> } | null = null;
    server.use(
      http.patch('*/api/v1/notifications/1', async ({ request }) => {
        captured = (await request.json()) as { config: Record<string, unknown> };
        return HttpResponse.json({ id: 1 });
      }),
    );

    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <AddNotificationModal
        provider={signalProvider({
          server: 'http://localhost:8080',
          sender_number: '+15550000000',
          recipient_type: 'numbers',
          numbers: '+15551111111',
        })}
        onClose={onClose}
      />,
    );

    await screen.findByDisplayValue('My Signal');
    await user.click(screen.getByRole('button', { name: /add number/i }));
    const numberInputs = screen.getAllByPlaceholderText('+15551234567');
    await user.type(numberInputs[numberInputs.length - 1], '+15552222222');

    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured!.config).toMatchObject({ numbers: '+15551111111,+15552222222' });
  });

  it('refuses to save without any recipient number', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <AddNotificationModal
        provider={signalProvider({
          server: 'http://localhost:8080',
          sender_number: '+15550000000',
          recipient_type: 'numbers',
          numbers: '',
        })}
        onClose={onClose}
      />,
    );

    await screen.findByDisplayValue('My Signal');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(onClose).not.toHaveBeenCalled();
  });

  it('refuses to save without a Group ID in group mode', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <AddNotificationModal
        provider={signalProvider({
          server: 'http://localhost:8080',
          sender_number: '+15550000000',
          recipient_type: 'group',
          group_id: '',
        })}
        onClose={onClose}
      />,
    );

    await screen.findByDisplayValue('My Signal');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(onClose).not.toHaveBeenCalled();
  });
});
