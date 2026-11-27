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
import { NotificationProviderCard } from '../../components/NotificationProviderCard';
import { NOTIFICATION_SECRET_FIELDS } from '../../components/notificationCredentials';
import { PROVIDER_EVENTS } from '../fixtures/providerEvents';
import type { NotificationProvider } from '../../api/client';

function buildProvider(provider_type: string, name: string, config: Record<string, unknown>) {
  const fields = NOTIFICATION_SECRET_FIELDS[provider_type as keyof typeof NOTIFICATION_SECRET_FIELDS] || [];
  return {
    id: 1,
    name,
    provider_type,
    enabled: true,
    config: Object.fromEntries(Object.entries(config).filter(([key]) => !fields.includes(key))),
    secrets_configured: Object.fromEntries(fields.map(key => [key, !!config[key]])),
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
    expect(screen.queryByDisplayValue('abc123')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Configured. Leave blank to keep.')).toHaveValue('');
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
      group: 'Printers',
      level: 'critical',
    });
    expect(captured!.config).not.toHaveProperty('device_key');
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
 * Gotify, a self-hosted push server (upstream Bambuddy #2743). Its token is a
 * secret like every other provider's, and it shares ntfy's per-event priority
 * section under its own title.
 */
describe('AddNotificationModal — Gotify', () => {
  const gotifyProvider = () =>
    ({
      ...buildProvider('gotify', 'My Gotify', { server: 'https://gotify.example.com', app_token: 'Atoken' }),
      on_print_failed: true,
    }) as unknown as NotificationProvider;

  it('shows the server and a masked token field', async () => {
    render(<AddNotificationModal provider={gotifyProvider()} onClose={() => undefined} />);

    expect(await screen.findByDisplayValue('https://gotify.example.com')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('Atoken')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Configured. Leave blank to keep.')).toHaveAttribute('type', 'password');
  });

  it('renders its own priority section and saves event_priorities', async () => {
    let captured: { config: Record<string, unknown>; secret_actions: Record<string, string> } | null = null;
    server.use(
      http.patch('*/api/v1/notifications/1', async ({ request }) => {
        captured = (await request.json()) as typeof captured;
        return HttpResponse.json({ id: 1 });
      }),
    );

    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={gotifyProvider()} onClose={onClose} />);

    const sectionHeader = await screen.findByText('Event priority (Gotify)');
    expect(screen.queryByText('Event priority (ntfy)')).not.toBeInTheDocument();
    const sectionRoot = sectionHeader.closest('div')!;
    await user.selectOptions(within(sectionRoot).getByRole('combobox'), '4');

    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured!.config).toMatchObject({
      server: 'https://gotify.example.com',
      event_priorities: { on_print_failed: 4 },
    });
    expect(captured!.config).not.toHaveProperty('app_token');
    expect(captured!.secret_actions).toEqual({ app_token: 'keep' });
  });
});

describe('AddNotificationModal — provider type list', () => {
  it('lists the providers alphabetically by their label', async () => {
    render(<AddNotificationModal onClose={() => undefined} />);

    await screen.findByRole('switch', { name: 'Print Started' });
    const select = screen.getAllByRole('combobox')[0] as HTMLSelectElement;
    const labels = Array.from(select.options).map((o) => o.textContent);

    expect(labels).toEqual([
      'Bark',
      'CallMeBot/WhatsApp',
      'Discord',
      'Email',
      'Gotify',
      'Home Assistant',
      'Notify!',
      'ntfy',
      'Pushover',
      'Signal CLI API',
      'Telegram',
      'Webhook',
    ]);
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


/**
 * Event subscriptions in the provider form.
 *
 * The form used to offer eighteen of the thirty-four flags the backend knows,
 * and to send only those eighteen. The other sixteen were filled in by the
 * backend's own defaults - six of which are ON - so a brand-new ntfy or Discord
 * provider started sending AMS, queue and plate events its creator had never
 * been shown, and could only switch them off on the expanded card. These tests
 * pin the two halves of the fix: everything is offered, and everything is sent.
 */
describe('AddNotificationModal - event subscriptions', () => {
  it('offers every event the API knows, not a subset', async () => {
    render(<AddNotificationModal onClose={() => undefined} />);

    await screen.findByRole('switch', { name: 'Print Started' });
    const subscriptionToggles = PROVIDER_EVENTS.length;
    expect(screen.getAllByRole('switch').length).toBeGreaterThanOrEqual(subscriptionToggles);
    // One from each group the old hand-written list left out entirely.
    expect(screen.getByRole('switch', { name: 'AMS Humidity High' })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Job Failed' })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Sensor readings' })).toBeInTheDocument();
  });

  it('shows a new provider the defaults it would be saved with', async () => {
    render(<AddNotificationModal onClose={() => undefined} />);

    // on_plate_not_empty defaults ON in the backend registry. Before the fix the
    // form showed nothing for it and the backend switched it on out of sight.
    const plate = await screen.findByRole('switch', { name: 'Plate Not Empty' });
    expect(plate).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('switch', { name: 'Print Started' })).toHaveAttribute('aria-checked', 'false');
  });

  it('sends every flag on save, so the backend fills none in silently', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(
      http.post('*/api/v1/notifications/', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 7 });
      }),
    );

    const user = userEvent.setup();
    render(<AddNotificationModal onClose={vi.fn()} />);

    await screen.findByRole('switch', { name: 'Print Started' });
    // ntfy: one required config field, so the save is about the events.
    await user.selectOptions(screen.getAllByRole('combobox')[0], 'ntfy');
    await user.type(screen.getByPlaceholderText('My Notifications'), 'Test');
    await user.type(screen.getByPlaceholderText('my-bamdude'), 'farm');
    await user.click(screen.getByRole('button', { name: /^add$/i }));

    await waitFor(() => expect(captured).not.toBeNull());
    const sent = Object.keys(captured!).filter((key) => key.startsWith('on_'));
    expect(sent.sort()).toEqual(PROVIDER_EVENTS.map((event) => event.flag).sort());
  });

  it('carries the edited provider own subscriptions into the form', async () => {
    render(
      <AddNotificationModal
        provider={buildProvider('ntfy', 'My ntfy', { topic: 'bamdude' })}
        onClose={() => undefined}
      />,
    );

    await screen.findByDisplayValue('My ntfy');
    // The fixture provider carries no on_* fields at all, so every flag shows
    // as off rather than inheriting a default meant for a new provider.
    const jobFailed = await screen.findByRole('switch', { name: 'Job Failed' });
    expect(jobFailed).toHaveAttribute('aria-checked', 'false');
  });

  it('refuses to save while the event list is unavailable', async () => {
    // Saving without the list would send no on_* fields at all, and the backend
    // would fill in its defaults — six of them ON. Better to refuse than to
    // reintroduce the bug quietly.
    server.use(http.get('*/api/v1/notifications/events', () => HttpResponse.error()));

    render(<AddNotificationModal onClose={() => undefined} />);

    await waitFor(() => expect(screen.getByRole('button', { name: /^add$/i })).toBeDisabled());
  });
});

/**
 * Notify! (upstream Bambuddy v1.3.0, #3265): push alerts, iOS Live Activities and Lock Screen
 * widgets. Upstream's tests, on our secret contract: the token is never shown, an edit keeps
 * it through ``secret_actions``, a test request carries no photo flag, and a provider names
 * its printers in a list.
 */
describe('AddNotificationModal — Notify!', () => {
  const notifyProvider = (config: Record<string, unknown> = {}) =>
    buildProvider('notify', 'My Notify!', { device_id: 'IO12345678901234', token: 'notify-secret', ...config });

  const liveActivitiesSwitch = () => within(
    screen.getByRole('group', { name: 'Live Activities (iOS)' }),
  ).getByRole('switch');

  const widgetsSwitch = () => within(
    screen.getByRole('group', { name: 'Lock Screen Widgets (iOS)' }),
  ).getByRole('switch');

  const photoSwitch = () => screen.getAllByRole('switch').find(
    (toggle) => toggle.parentElement?.textContent?.includes('Attach photos'),
  )!;

  it('offers Notify! with masked credentials and Live Activities disabled by default', async () => {
    const user = userEvent.setup();
    render(<AddNotificationModal onClose={() => undefined} />);
    await screen.findByRole('switch', { name: 'Print Started' });
    await user.selectOptions(screen.getAllByRole('combobox')[0], 'notify');

    expect(screen.getByLabelText(/device or group id/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/notify! token/i)).toHaveAttribute('type', 'password');
    expect(liveActivitiesSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(widgetsSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByText('Live Activity appearance')).not.toBeInTheDocument();
    expect(screen.getByText(/Use an iOS device ID; add a separate provider for group pushes/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /test configuration/i })).toBeDisabled();
    await user.type(screen.getByLabelText(/device or group id/i), 'GRP12345678901234');
    expect(screen.getByRole('button', { name: /test configuration/i })).toBeDisabled();
  });

  it('creates a group push provider with Live Activities explicitly disabled', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(http.post('*/api/v1/notifications/', async ({ request }) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 2 });
    }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal onClose={onClose} />);
    await screen.findByRole('switch', { name: 'Print Started' });
    await user.selectOptions(screen.getAllByRole('combobox')[0], 'notify');
    await user.type(screen.getByPlaceholderText(/My Notifications/i), 'Notify! group');
    await user.type(screen.getByLabelText(/device or group id/i), 'GRP12345678901234');
    await user.type(screen.getByLabelText(/notify! token/i), 'secret');
    await user.click(screen.getByRole('button', { name: /^add$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured).toMatchObject({
      provider_type: 'notify',
      config: { device_id: 'GRP12345678901234', token: 'secret', live_activities: false },
      secret_actions: { token: 'replace' },
    });
  });

  it('saves tile customization with boolean and array types without enabling push events', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(http.patch('*/api/v1/notifications/1', async ({ request }) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 1 });
    }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={notifyProvider()} onClose={onClose} />);
    await screen.findByDisplayValue('My Notify!');
    await user.click(liveActivitiesSwitch());
    expect(screen.getByText(/Event toggles below control push alerts only/)).toBeInTheDocument();
    expect(screen.getByText(/Quiet hours block new activities; existing activities still update and end/)).toBeInTheDocument();
    await user.click(screen.getByText('Live Activity appearance'));
    await user.selectOptions(screen.getByLabelText('Hide file name on tile'), 'true');
    await user.selectOptions(screen.getByLabelText('Show print stage'), 'true');
    await user.selectOptions(screen.getByLabelText('Progress style'), 'segments');
    await user.selectOptions(screen.getByLabelText('Time Sensitive problem alerts'), 'true');
    await user.click(screen.getByRole('checkbox', { name: 'ETA' }));
    await user.click(screen.getByRole('checkbox', { name: 'Bed' }));
    await user.type(screen.getByLabelText('Dashboard URL (optional)'), 'https://bamdude.example.com');
    await user.type(screen.getByLabelText('Tint (optional)'), '#123456');
    await user.type(screen.getByLabelText('Symbol (optional)'), 'printer');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured).toMatchObject({
      on_print_start: false,
      on_print_progress: false,
      secret_actions: { token: 'keep' },
      config: {
        live_activities: true,
        live_activity_privacy: true,
        live_activity_stage: true,
        live_activity_style: 'segments',
        live_activity_metrics: ['eta', 'bed'],
        live_activity_button_url: 'https://bamdude.example.com',
        live_activity_tint: '#123456',
        live_activity_symbol: 'printer',
        time_sensitive: true,
      },
    });
    expect((captured as unknown as { config: Record<string, unknown> }).config).not.toHaveProperty('token');
  });

  it('preserves existing customization while disabling Live Activities', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(http.patch('*/api/v1/notifications/1', async ({ request }) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 1 });
    }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={notifyProvider({
      live_activities: true,
      live_activity_privacy: true,
      live_activity_style: 'none',
      live_activity_metrics: ['layers', 'nozzle'],
      time_sensitive: false,
    })} onClose={onClose} />);
    await screen.findByDisplayValue('My Notify!');
    expect(liveActivitiesSwitch()).toHaveAttribute('aria-checked', 'true');
    await user.click(screen.getByText('Live Activity appearance'));
    expect(screen.getByLabelText('Hide file name on tile')).toHaveValue('true');
    expect(screen.getByLabelText('Progress style')).toHaveValue('none');
    expect(screen.getByRole('checkbox', { name: 'Layer' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Nozzle' })).toBeChecked();
    await user.click(liveActivitiesSwitch());
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured).toMatchObject({ config: {
      live_activities: false,
      live_activity_privacy: true,
      live_activity_style: 'none',
      live_activity_metrics: ['layers', 'nozzle'],
      time_sensitive: false,
    } });
  });

  it.each(['GRP12345678901234', 'wb12345678901234', 'MC12345678901234'])('keeps %s eligible for pushes with Live Activities disabled', async (deviceId) => {
    let captured: Record<string, unknown> | null = null;
    server.use(http.patch('*/api/v1/notifications/1', async ({ request }) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 1 });
    }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={notifyProvider({ device_id: deviceId, live_activities: true, lock_screen_widgets: true })} onClose={onClose} />);
    await screen.findByDisplayValue('My Notify!');
    expect(liveActivitiesSwitch()).toBeDisabled();
    expect(liveActivitiesSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(widgetsSwitch()).toBeDisabled();
    expect(widgetsSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(/Group, browser and macOS IDs use push notifications only/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /test configuration/i })).toBeEnabled();
    expect(screen.queryByText('Live Activity appearance')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured).toMatchObject({
      config: { device_id: deviceId, live_activities: false, lock_screen_widgets: false },
      secret_actions: { token: 'keep' },
    });
  });

  it.each(['GRP12345678901234', 'WB12345678901234'])('turns off Live Activities when changing an iOS ID to %s', async (deviceId) => {
    let captured: Record<string, unknown> | null = null;
    server.use(http.post('*/api/v1/notifications/1/test-config', async ({ request }) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ success: true, message: 'Push delivered' });
    }));
    const user = userEvent.setup();
    render(<AddNotificationModal provider={notifyProvider({ live_activities: true, lock_screen_widgets: true })} onClose={() => undefined} />);
    await screen.findByDisplayValue('My Notify!');
    expect(liveActivitiesSwitch()).toHaveAttribute('aria-checked', 'true');
    expect(widgetsSwitch()).toHaveAttribute('aria-checked', 'true');
    const target = screen.getByLabelText(/device or group id/i);
    await user.clear(target);
    await user.type(target, deviceId);
    expect(liveActivitiesSwitch()).toBeDisabled();
    expect(liveActivitiesSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(widgetsSwitch()).toBeDisabled();
    expect(widgetsSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(photoSwitch()).toBeDisabled();
    expect(photoSwitch()).toHaveAttribute('aria-checked', 'false');
    await user.click(screen.getByRole('button', { name: /test configuration/i }));
    expect(await screen.findByText('Push delivered')).toBeInTheDocument();
    // Ours: a test request never carries a photo flag (the test notification has no photo).
    expect(captured).not.toHaveProperty('attach_photo');
    expect(captured).toMatchObject({ config: {
      device_id: deviceId,
      live_activities: false,
      lock_screen_widgets: false,
    } });
    await user.clear(target);
    await user.type(target, 'IO12345678901234');
    expect(liveActivitiesSwitch()).toBeEnabled();
    expect(liveActivitiesSwitch()).toHaveAttribute('aria-checked', 'false');
  });

  it('sends setup tests through the push test endpoint with typed config', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(http.post('*/api/v1/notifications/1/test-config', async ({ request }) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ success: true, message: 'Push delivered' });
    }));
    const user = userEvent.setup();
    render(<AddNotificationModal provider={notifyProvider({ live_activities: true })} onClose={() => undefined} />);
    await screen.findByDisplayValue('My Notify!');
    expect(screen.getByText(/Testing sends a push notification/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /test configuration/i }));
    expect(await screen.findByText('Push delivered')).toBeInTheDocument();
    expect(captured).toMatchObject({
      provider_type: 'notify',
      config: { device_id: 'IO12345678901234', live_activities: true, live_activity_metrics: [] },
      secret_actions: { token: 'keep' },
    });
  });

  it('clears Notify customization when changing provider type', async () => {
    const user = userEvent.setup();
    render(<AddNotificationModal onClose={() => undefined} />);
    await screen.findByRole('switch', { name: 'Print Started' });
    const providerSelect = screen.getAllByRole('combobox')[0];
    await user.selectOptions(providerSelect, 'notify');
    await user.click(liveActivitiesSwitch());
    await user.click(screen.getByText('Live Activity appearance'));
    await user.click(screen.getByRole('checkbox', { name: 'ETA' }));
    await user.selectOptions(providerSelect, 'ntfy');
    await user.selectOptions(providerSelect, 'notify');
    expect(liveActivitiesSwitch()).toHaveAttribute('aria-checked', 'false');
    await user.click(liveActivitiesSwitch());
    await user.click(screen.getByText('Live Activity appearance'));
    expect(screen.getByRole('checkbox', { name: 'ETA' })).not.toBeChecked();
  });

  it('defaults to the timer with optional metrics unchecked', async () => {
    const user = userEvent.setup();
    render(<AddNotificationModal provider={notifyProvider({ live_activities: true })} onClose={() => undefined} />);
    await screen.findByDisplayValue('My Notify!');
    await user.click(screen.getByText('Live Activity appearance'));
    expect(screen.getByText(/The timer is shown by default/)).toBeInTheDocument();
    for (const metric of ['Progress', 'ETA', 'Layer', 'Nozzle', 'Bed', 'Chamber']) {
      expect(screen.getByRole('checkbox', { name: metric })).not.toBeChecked();
    }
  });

  it('enables Lock Screen widgets independently of Live Activities and push events', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(http.patch('*/api/v1/notifications/1', async ({ request }) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 1 });
    }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={notifyProvider()} onClose={onClose} />);
    await screen.findByDisplayValue('My Notify!');
    expect(screen.getByText(/iOS refreshes roughly every 15 minutes or longer/)).toBeInTheDocument();
    // Ours (A3): a widget is for an available printer -- active and not archived.
    expect(screen.getByText(/Only active, non-archived printers receive widgets/)).toBeInTheDocument();
    expect(screen.getByText(/Notify! allows 10 widgets per device in total/)).toBeInTheDocument();
    await user.click(widgetsSwitch());
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured).toMatchObject({
      on_print_start: false,
      on_print_progress: false,
      printer_ids: null,
      config: { live_activities: false, lock_screen_widgets: true },
      secret_actions: { token: 'keep' },
    });
  });

  it.each(['GRP12345678901234', 'WB12345678901234'])('saves %s with photo attachments disabled', async (deviceId) => {
    let captured: Record<string, unknown> | null = null;
    server.use(http.patch('*/api/v1/notifications/1', async ({ request }) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 1 });
    }));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={notifyProvider({ device_id: deviceId })} onClose={onClose} />);
    await screen.findByDisplayValue('My Notify!');
    expect(photoSwitch()).toBeDisabled();
    expect(photoSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(/Browser and group recipients support text notifications only/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured).toMatchObject({ attach_photo: false, config: { device_id: deviceId } });
  });

  it.each(['GRP12345678901234', 'WB12345678901234'])('disables photo attachments in the %s provider card', async (deviceId) => {
    const user = userEvent.setup();
    render(<NotificationProviderCard provider={notifyProvider({ device_id: deviceId, live_activities: true, lock_screen_widgets: true })} onEdit={() => undefined} />);
    expect(screen.queryByText('Live Activities (iOS)')).not.toBeInTheDocument();
    expect(screen.queryByText('Lock Screen Widgets (iOS)')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /event settings/i }));
    expect(photoSwitch()).toBeDisabled();
    expect(photoSwitch()).toHaveAttribute('aria-checked', 'false');
  });

  it('shows a separate badge when Lock Screen widgets are enabled', () => {
    render(<NotificationProviderCard provider={notifyProvider({ lock_screen_widgets: true })} onEdit={() => undefined} />);
    expect(screen.getByText('Lock Screen Widgets (iOS)')).toBeInTheDocument();
    expect(screen.queryByText('Live Activities (iOS)')).not.toBeInTheDocument();
  });

  it('shows the Live Activities badge on enabled Notify! provider cards', () => {
    render(<NotificationProviderCard provider={notifyProvider({ live_activities: true })} onEdit={() => undefined} />);
    expect(screen.getByText('Notify!')).toBeInTheDocument();
    expect(screen.getByText('Live Activities (iOS)')).toBeInTheDocument();
  });

  it('does not show the Live Activities badge for push-only providers', () => {
    render(<NotificationProviderCard provider={notifyProvider({ live_activities: false })} onEdit={() => undefined} />);
    expect(screen.queryByText('Live Activities (iOS)')).not.toBeInTheDocument();
  });
});
