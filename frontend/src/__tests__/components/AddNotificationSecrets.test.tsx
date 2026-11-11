import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { AddNotificationModal } from '../../components/AddNotificationModal';
import type { NotificationProvider } from '../../api/client';

const provider = (extra: Partial<NotificationProvider> = {}) => ({
  id: 17, name: 'Private phone', provider_type: 'bark', enabled: true,
  config: { server: 'https://push.example/base' }, secrets_configured: { device_key: true },
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', ...extra,
} as NotificationProvider);

describe('Notification secret editing', () => {
  it.each([
    ['callmebot', {phone:'+15551234567'}, ['apikey']],
    ['ntfy', {server:'https://ntfy.example'}, ['topic','auth_token']],
    ['pushover', {}, ['user_key','app_token']],
    ['bark', {}, ['device_key']],
    ['telegram', {}, ['bot_token']],
    ['email', {smtp_server:'smtp.example', username:'sender', from_email:'from@example.com', to_email:'to@example.com'}, ['password']],
    ['discord', {}, ['webhook_url']],
    ['webhook', {}, ['webhook_url','auth_header']],
    ['homeassistant', {service:'notify.phone'}, []],
    ['signal', {server:'https://signal.example', sender_number:'+15551234567', numbers:'+15559876543'}, ['auth_header']],
  ] as const)('preserves configured credentials for %s without a readable value', async (kind, config, fields) => {
    let captured: Record<string, unknown> | undefined;
    server.use(http.patch('*/api/v1/notifications/17', async ({request}) => {
      captured = await request.json() as Record<string, unknown>;
      return HttpResponse.json({id:17});
    }));
    const user = userEvent.setup();
    render(<AddNotificationModal provider={provider({provider_type:kind, config, secrets_configured:Object.fromEntries(fields.map(field=>[field,true]))})} onClose={()=>undefined} />);
    await user.click(await screen.findByRole('button', {name:/^save$/i}));
    await waitFor(()=>expect(captured).toBeDefined());
    expect(captured!.secret_actions).toEqual(Object.fromEntries(fields.map(field=>[field,'keep'])));
    for(const field of fields) expect(captured!.config).not.toHaveProperty(field);
  });

  it('keeps opaque fields when renaming and sends an addressed draft without a display mask', async () => {
    const saved: Record<string, unknown>[] = [];
    const drafts: Record<string, unknown>[] = [];
    server.use(
      http.patch('*/api/v1/notifications/17', async ({ request }) => {
        saved.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ id: 17 });
      }),
      http.post('*/api/v1/notifications/17/test-config', async ({ request }) => {
        drafts.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ success: true, message: 'sent' });
      }),
    );
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<AddNotificationModal provider={provider()} onClose={onClose} />);
    expect(await screen.findByPlaceholderText('Configured. Leave blank to keep.')).toHaveValue('');
    await user.click(screen.getByRole('button', { name: /test configuration/i }));
    await waitFor(() => expect(drafts).toHaveLength(1));
    expect(drafts[0]).toMatchObject({ provider_id: 17, secret_actions: { device_key: 'keep' } });
    expect(drafts[0].config).not.toHaveProperty('device_key');
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(saved[0].secret_actions).toEqual({ device_key: 'keep' });
    expect(saved[0].config).not.toHaveProperty('device_key');
  });

  it('supports explicit clear and replacement after selecting clear', async () => {
    const saved: Record<string, unknown>[] = [];
    server.use(http.patch('*/api/v1/notifications/17', async ({ request }) => {
      saved.push(await request.json() as Record<string, unknown>);
      return HttpResponse.json({ id: 17 });
    }));
    const user = userEvent.setup();
    render(<AddNotificationModal provider={provider({ enabled: false })} onClose={() => undefined} />);
    await user.click(await screen.findByRole('button', { name: 'Clear stored credential' }));
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0].secret_actions).toEqual({ device_key: 'clear' });
    expect(saved[0].config).not.toHaveProperty('device_key');
    await user.click(screen.getByPlaceholderText('Configured. Leave blank to keep.'));
    await user.paste('actual***replacement');
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(saved).toHaveLength(2));
    expect(saved[1].secret_actions).toEqual({ device_key: 'replace' });
    expect(saved[1].config).toHaveProperty('device_key', 'actual***replacement');
  });

  it('keeps a credential-bearing legacy server instead of substituting the public default', async () => {
    let draft: Record<string, unknown> | undefined;
    server.use(http.post('*/api/v1/notifications/17/test-config', async ({ request }) => {
      draft = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ success: true, message: 'sent' });
    }));
    const user = userEvent.setup();
    render(<AddNotificationModal provider={provider({config: {}, secrets_configured: {device_key: true, server: true}})} onClose={() => undefined} />);
    expect(await screen.findAllByPlaceholderText('Configured. Leave blank to keep.')).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: /test configuration/i }));
    await waitFor(() => expect(draft).toBeDefined());
    expect(draft!.secret_actions).toEqual({device_key: 'keep', server: 'keep'});
    expect(draft!.config).not.toHaveProperty('server');
  });

  it('turns email authentication off while preserving the bound username and stored password', async () => {
    let saved: Record<string, unknown> | undefined;
    server.use(http.patch('*/api/v1/notifications/17', async ({ request }) => {
      saved = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 17 });
    }));
    const user = userEvent.setup();
    render(<AddNotificationModal provider={provider({
      provider_type: 'email',
      config: { smtp_server: 'smtp.example', username: 'sender', auth_enabled: 'true', from_email: 'from@example.com', to_email: 'to@example.com' },
      secrets_configured: { password: true },
    })} onClose={() => undefined} />);
    await user.selectOptions(screen.getByDisplayValue('Enabled'), 'false');
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(saved).toBeDefined());
    expect(saved!.config).toMatchObject({ auth_enabled: 'false', username: 'sender' });
    expect(saved!.config).not.toHaveProperty('password');
    expect(saved!.secret_actions).toEqual({ password: 'keep' });
  });
});
