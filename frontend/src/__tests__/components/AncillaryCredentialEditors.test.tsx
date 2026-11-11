import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { AddSmartPlugModal } from '../../components/AddSmartPlugModal';
import { OIDCProviderSettings } from '../../components/OIDCProviderSettings';
import { GitBackupSettings } from '../../components/GitBackupSettings';
import type { SmartPlug } from '../../api/client';

const plug = { id: 7, name: 'Bench', plug_type: 'tasmota', ip_address: '10.0.0.5', enabled: true,
  configured_secrets: { password: true }, password: 'LEGACY_SHOULD_NOT_PREFILL' } as unknown as SmartPlug;

describe('Ancillary write-only credential editors', () => {
  it('keeps an untouched Tasmota password omitted and never prefills old response data', async () => {
    const sent = vi.fn();
    server.use(http.patch('/api/v1/smart-plugs/7', async ({ request }) => {
      sent(await request.json()); return HttpResponse.json(plug);
    }));
    const { container } = render(<AddSmartPlugModal plug={plug} onClose={() => {}} />);
    expect(container.querySelector('[value="LEGACY_SHOULD_NOT_PREFILL"]')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }));
    await waitFor(() => expect(sent).toHaveBeenCalled());
    expect(sent.mock.calls[0][0]).toMatchObject({ secret_actions: { password: 'keep' } });
    expect(sent.mock.calls[0][0]).not.toHaveProperty('password');
    expect(JSON.stringify(sent.mock.calls)).not.toContain('LEGACY_SHOULD_NOT_PREFILL');
  });

  it('carries an explicit Tasmota clear independently of the blank input', async () => {
    const sent = vi.fn();
    server.use(http.patch('/api/v1/smart-plugs/7', async ({ request }) => {
      sent(await request.json()); return HttpResponse.json(plug);
    }));
    render(<AddSmartPlugModal plug={plug} onClose={() => {}} />);
    fireEvent.click(screen.getByRole('checkbox', { name: /Remove saved Password/i }));
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }));
    await waitFor(() => expect(sent).toHaveBeenCalled());
    expect(sent.mock.calls[0][0]).toMatchObject({ enabled: false, secret_actions: { password: 'clear' } });
    expect(sent.mock.calls[0][0]).not.toHaveProperty('password');
  });

  it('disables OIDC and omits the value when removing its saved secret', async () => {
    const provider = { id: 1, name: 'Private IdP', issuer_url: 'https://id.example', client_id: 'app',
      client_secret_configured: true, scopes: 'openid email profile', is_enabled: true,
      auto_create_users: false, email_claim: 'email', require_email_verified: true, is_autologin: false };
    const sent = vi.fn();
    server.use(
      http.get('/api/v1/auth/oidc/providers/all', () => HttpResponse.json([provider])),
      http.get('/api/v1/auth/groups', () => HttpResponse.json([])),
      http.put('/api/v1/auth/oidc/providers/1', async ({ request }) => {
        sent(await request.json()); return HttpResponse.json(provider);
      }),
    );
    render(<OIDCProviderSettings />);
    fireEvent.click(await screen.findByTestId('edit-provider-1'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove secret and disable provider' }));
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }));
    await waitFor(() => expect(sent).toHaveBeenCalled());
    expect(sent.mock.calls[0][0]).toMatchObject({ is_enabled: false, secret_actions: { client_secret: 'clear' } });
    expect(sent.mock.calls[0][0]).not.toHaveProperty('client_secret');
  });

  it('switches clear back to explicit replacement when a new password is entered', async () => {
    const sent = vi.fn();
    server.use(http.patch('/api/v1/smart-plugs/7', async ({ request }) => {
      sent(await request.json()); return HttpResponse.json(plug);
    }));
    render(<AddSmartPlugModal plug={plug} onClose={() => {}} />);
    const clear = screen.getByRole('checkbox', { name: /Remove saved Password/i });
    fireEvent.click(clear);
    fireEvent.change(document.querySelector('input[type="password"]')!, { target: { value: '*literal-password*' } });
    expect(clear).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }));
    await waitFor(() => expect(sent).toHaveBeenCalled());
    expect(sent.mock.calls[0][0]).toMatchObject({ password: '*literal-password*', secret_actions: { password: 'replace' } });
    expect(sent.mock.calls[0][0].enabled).not.toBe(false);
  });

  it('tests the current Git draft with keep rather than silently testing the saved destination', async () => {
    const sent = vi.fn();
    const stored = vi.fn();
    const saves = vi.fn();
    server.use(
      http.get('/api/v1/git-backup/config', () => HttpResponse.json({ id: 1, provider: 'github',
        repository_url: 'https://github.com/owner/private', has_token: true, branch: 'main', enabled: true, api_base_url_private: true,
        schedule_enabled: false, schedule_type: 'daily' })),
      http.get('/api/v1/git-backup/status', () => HttpResponse.json({ configured: true, enabled: true, is_running: false })),
      http.get('/api/v1/git-backup/logs', () => HttpResponse.json([])),
      http.get('/api/v1/local-backup/status', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/local-backup/backups', () => HttpResponse.json([])),
      http.get('/api/v1/cloud/status', () => HttpResponse.json({ is_authenticated: false })),
      http.post('/api/v1/git-backup/test', async ({ request }) => {
        sent(await request.json()); return HttpResponse.json({ success: false, message: 'Replace token for this destination' });
      }),
      http.post('/api/v1/git-backup/test-stored', () => { stored(); return HttpResponse.json({ success: true }); }),
      http.patch('/api/v1/git-backup/config', () => { saves(); return HttpResponse.json({ detail: 'Replace token' }, { status: 400 }); }),
      http.post('/api/v1/git-backup/config', () => { saves(); return HttpResponse.json({ detail: 'Unexpected save' }, { status: 400 }); }),
    );
    render(<GitBackupSettings />);
    const repository = await screen.findByDisplayValue('https://github.com/owner/private');
    fireEvent.change(repository, { target: { value: 'https://github.com/owner/draft' } });
    await act(() => new Promise(resolve => setTimeout(resolve, 1500)));
    fireEvent.click(screen.getByRole('button', { name: /^Test$/ }));
    await waitFor(() => expect(sent).toHaveBeenCalled());
    expect(sent.mock.calls[0][0]).toMatchObject({ repo_url: 'https://github.com/owner/draft', secret_actions: { token: 'keep' } });
    expect(sent.mock.calls[0][0]).not.toHaveProperty('token');
    expect(sent.mock.calls[0][0]).not.toHaveProperty('api_base_url');
    expect(stored).not.toHaveBeenCalled();
    await act(() => new Promise(resolve => setTimeout(resolve, 1500)));
    expect(saves).not.toHaveBeenCalled();
  }, 15000);

  it('keeps a Git token draft private across metadata autosave and saves the pair explicitly by PATCH', async () => {
    let config = { id: 1, provider: 'github', repository_url: 'https://github.com/owner/private', has_token: true,
      api_base_url: null, api_base_url_private: true, branch: 'main', enabled: true, schedule_enabled: false,
      schedule_type: 'daily' };
    const writes: Record<string, unknown>[] = [];
    const posts = vi.fn();
    const probes = vi.fn();
    server.use(
      http.get('/api/v1/git-backup/config', () => HttpResponse.json(config)),
      http.get('/api/v1/git-backup/status', () => HttpResponse.json({ configured: true, enabled: true, is_running: false })),
      http.get('/api/v1/git-backup/logs', () => HttpResponse.json([])),
      http.get('/api/v1/local-backup/status', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/local-backup/backups', () => HttpResponse.json([])),
      http.get('/api/v1/cloud/status', () => HttpResponse.json({ is_authenticated: false })),
      http.post('/api/v1/git-backup/config', () => { posts(); return HttpResponse.json(config); }),
      http.patch('/api/v1/git-backup/config', async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        writes.push(body);
        config = { ...config, ...(typeof body.branch === 'string' ? { branch: body.branch } : {}),
          ...(typeof body.repository_url === 'string' ? { repository_url: body.repository_url } : {}) };
        return HttpResponse.json(config);
      }),
      http.post('/api/v1/git-backup/test', async ({ request }) => {
        probes(await request.json()); return HttpResponse.json({ success: true, message: 'Connected' });
      }),
    );
    render(<GitBackupSettings />);
    const repository = await screen.findByDisplayValue('https://github.com/owner/private');
    fireEvent.change(repository, { target: { value: 'https://github.com/owner/draft' } });
    fireEvent.change(document.querySelector('input[type="password"]')!, { target: { value: 'NEW_DRAFT_TOKEN' } });
    await act(() => new Promise(resolve => setTimeout(resolve, 1500)));
    expect(writes).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: /^Test$/ }));
    await waitFor(() => expect(probes).toHaveBeenCalledOnce());
    await act(() => new Promise(resolve => setTimeout(resolve, 1500)));
    expect(writes).toHaveLength(0);
    fireEvent.change(screen.getByDisplayValue('main'), { target: { value: 'metadata-branch' } });
    await waitFor(() => expect(writes).toHaveLength(1));
    for (const field of ['provider', 'repository_url', 'api_base_url', 'access_token']) expect(writes[0]).not.toHaveProperty(field);
    expect(repository).toHaveValue('https://github.com/owner/draft');
    expect(document.querySelector('input[type="password"]')).toHaveValue('NEW_DRAFT_TOKEN');
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(writes[1]).toMatchObject({ repository_url: 'https://github.com/owner/draft', access_token: 'NEW_DRAFT_TOKEN', secret_actions: { access_token: 'replace' } });
    expect(writes[1]).not.toHaveProperty('api_base_url');
    expect(posts).not.toHaveBeenCalled();
  }, 15000);
});
