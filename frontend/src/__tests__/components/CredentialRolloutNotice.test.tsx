import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { CredentialRolloutNotice, RetiredCredentialNotice } from '../../components/settings/CredentialRolloutNotice';
import CameraTokensPanel from '../../components/settings/CameraTokensPanel';
import { SettingsPage } from '../../pages/SettingsPage';

let isAdmin = true;
let denied = new Set<string>();
vi.mock('../../contexts/AuthContext', async importOriginal => ({
  ...await importOriginal<typeof import('../../contexts/AuthContext')>(),
  useAuth: () => ({ isAdmin, authEnabled: true, user: { id: 1, username: 'admin' }, hasPermission: (permission: string) => !denied.has(permission) }),
}));

const rollout = {
  contract_key: 'g01-credential-contract-v1', episode_id: 'episode',
  ownerless_api_key_count: 2, owned_api_key_count: 3, long_lived_token_count: 4,
  created_at: '2026-11-12T00:00:00', delivered_at: null, notification_state: 'pending', runtime_phase: 'pending',
};
const retiredToken = { id: 9, token_identity: '11111111-1111-4111-8111-111111111111', user_id: 1, name: 'Old TV', scope: 'monitor', lookup_prefix: 'bdll_old',
  created_at: '2026-01-01T00:00:00', expires_at: null, last_used_at: null, token: null,
  retired_reason: 'legacy_credential_reissue_required', retired_at: '2026-11-12T00:00:00', revoked_at: '2026-11-12T00:00:00' };

describe('Credential rollout visibility', () => {
  beforeEach(() => {
    isAdmin = true;
    denied = new Set();
    window.history.replaceState({}, '', '/');
    server.use(
      http.get('/api/v1/api-keys/rollout', () => HttpResponse.json(rollout)),
      http.get('/api/v1/auth/tokens', () => HttpResponse.json([])),
      http.get('/api/v1/auth/tokens/all', () => HttpResponse.json([])),
    );
  });

  it('shows historical counts and the integration checklist independently of inbox state', async () => {
    render(<CredentialRolloutNotice />);
    const notice = await screen.findByRole('region', { name: 'Integration credentials need reissuing' });
    expect(within(notice).getByText(/3 owned API keys, 2 ownerless API keys and 4 camera\/monitor tokens/)).toBeInTheDocument();
    expect(within(notice).getByText(/HA, webhooks\/scripts, bridge, MCP\/AI host settings and OrcaBamDude/)).toBeInTheDocument();
    expect(within(notice).getByText(/camera streams, camera wall, overlay, monitor, TV, OBS, kiosk and launcher links/)).toBeInTheDocument();
    expect(within(notice).getByText(/Sign in again in each browser and regenerate its derived links/)).toBeInTheDocument();
    expect(within(notice).getByText(/Browser sign-in alone does not reconnect integrations/)).toBeInTheDocument();
  });

  it.each([
    ['pending', 'Administrator inbox notice: pending.'],
    ['delivered', 'Administrator inbox notice: completed. Subscription opt-outs are respected.'],
  ])('shows the finite %s inbox state independently of retained counts', async (state, message) => {
    server.use(http.get('/api/v1/api-keys/rollout', () => HttpResponse.json({ ...rollout, notification_state: state })));
    render(<CredentialRolloutNotice />);
    const notice = await screen.findByRole('region', { name: 'Integration credentials need reissuing' });
    expect(within(notice).getByText(message)).toBeInTheDocument();
    expect(within(notice).getByText(/3 owned API keys, 2 ownerless API keys and 4 camera\/monitor tokens/)).toBeInTheDocument();
  });

  it.each(['not-admin', 'no-api-keys-read'])('does not query or display status for %s', async mode => {
    let calls = 0;
    if (mode === 'not-admin') isAdmin = false;
    else denied.add('api_keys:read');
    server.use(http.get('/api/v1/api-keys/rollout', () => { calls++; return HttpResponse.json(rollout); }));
    render(<CredentialRolloutNotice />);
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(calls).toBe(0);
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
  });

  it('has no empty warning on a fresh installation', async () => {
    server.use(http.get('/api/v1/api-keys/rollout', () => HttpResponse.json({ ...rollout, episode_id: null, notification_state: 'none' })));
    render(<CredentialRolloutNotice />);
    await waitFor(() => expect(screen.queryByRole('region')).not.toBeInTheDocument());
  });

  it('uses finite copy for an unknown reason rather than reflecting server prose', () => {
    render(<RetiredCredentialNotice reason="CANARY_RAW_SERVER_REASON" />);
    expect(screen.getByText('Needs reissue')).toBeInTheDocument();
    expect(screen.queryByText(/CANARY_RAW/)).not.toBeInTheDocument();
  });

  it('removes a retired monitor record only after confirmation and preserves the new-token form', async () => {
    let removed = false;
    server.use(
      http.get('/api/v1/auth/tokens', () => HttpResponse.json(removed ? [] : [retiredToken])),
      http.delete('/api/v1/auth/tokens/9', ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get('expected_identity')).toBe(retiredToken.token_identity);
        expect(url.searchParams.get('retired_only')).toBe('true');
        removed = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    render(<CameraTokensPanel />);
    const row = (await screen.findByText('Old TV')).closest('tr')!;
    expect(within(row).getByText('Needs reissue')).toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Delete' }));
    expect(removed).toBe(false);
    const dialog = await screen.findByRole('dialog', { name: 'Remove this token record?' });
    expect(within(dialog).getByText(/This token is already inactive/)).toBeInTheDocument();
    expect(within(row).getByText(/Use the create form above/)).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.queryByText('Old TV')).not.toBeInTheDocument());
    expect(removed).toBe(true);
  });

  it('requires the existing delete permission to remove retired monitor metadata', async () => {
    denied.add('api_keys:delete');
    server.use(http.get('/api/v1/auth/tokens', () => HttpResponse.json([retiredToken])));
    render(<CameraTokensPanel />);
    const row = (await screen.findByText('Old TV')).closest('tr')!;
    expect(within(row).getByRole('button', { name: 'Delete' })).toBeDisabled();
  });

  it.each([false, true])('does not mutate a list entry missing its immutable identity (retired=%s)', async retired => {
    server.use(http.get('/api/v1/auth/tokens', () => HttpResponse.json([{
      ...retiredToken, token_identity: undefined,
      retired_reason: retired ? retiredToken.retired_reason : null,
      retired_at: retired ? retiredToken.retired_at : null,
    }])));
    render(<CameraTokensPanel />);
    const row = (await screen.findByText('Old TV')).closest('tr')!;
    expect(within(row).getByRole('button', { name: retired ? 'Delete' : 'Revoke' })).toBeDisabled();
  });

  it('pins a current revoke to the selected immutable identity too', async () => {
    let revoked = false;
    server.use(
      http.get('/api/v1/auth/tokens', () => HttpResponse.json(revoked ? [] : [{
        ...retiredToken, retired_reason: null, retired_at: null, revoked_at: null,
      }])),
      http.delete('/api/v1/auth/tokens/9', ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get('expected_identity')).toBe(retiredToken.token_identity);
        expect(url.searchParams.has('retired_only')).toBe(false);
        revoked = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    render(<CameraTokensPanel />);
    fireEvent.click(within((await screen.findByText('Old TV')).closest('tr')!).getByRole('button', { name: 'Revoke' }));
    const dialog = await screen.findByRole('dialog', { name: 'Revoke this token?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(screen.queryByText('Old TV')).not.toBeInTheDocument());
    expect(revoked).toBe(true);
  });

  it('shows ownerless retired API key and starts a fresh restricted draft without copying it', async () => {
    let created: Record<string, unknown> | undefined;
    window.history.replaceState({}, '', '/settings?tab=apikeys');
    server.use(http.post('/api/v1/api-keys/', async ({ request }) => {
      created = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 99, ...created, key: 'bd_NEW_ONCE_ONLY' });
    }), http.get('/api/v1/api-keys/', () => HttpResponse.json([{
      id: 7, name: 'Legacy root key', key_prefix: 'bd_old', user_id: null, enabled: false,
      can_queue: true, can_control_printer: true, can_read_status: true, can_access_cloud: true,
      can_manage_library: true, can_manage_inventory: true, can_manage_maintenance: true,
      can_manage_archives: true, can_manage_projects: true, can_print_labels: true,
      created_at: '2026-01-01T00:00:00', last_used: null, expires_at: null,
      retired_reason: 'legacy_ownerless_reissue_required', retired_at: '2026-11-12T00:00:00',
    }])));
    render(<SettingsPage />);
    const button = await screen.findByRole('button', { name: 'Create a new key' });
    expect(screen.getByText(/This legacy key has no owner/)).toBeInTheDocument();
    fireEvent.click(button);
    const createHeading = screen.getByRole('heading', { name: /Create New API Key/i });
    const form = createHeading.closest('[class*="rounded"]')!;
    expect(within(form as HTMLElement).getByRole('textbox')).toHaveValue('');
    const toggles = within(form as HTMLElement).getAllByRole('checkbox');
    expect(toggles.filter(toggle => (toggle as HTMLInputElement).checked)).toHaveLength(1);
    fireEvent.click(within(form as HTMLElement).getByRole('button', { name: 'Create Key' }));
    await screen.findByText('bd_NEW_ONCE_ONLY');
    expect(created?.name).not.toBe('Legacy root key');
    expect(created).not.toHaveProperty('user_id');
    expect(created).not.toHaveProperty('id');
    expect(created).not.toHaveProperty('retired_reason');
    for (const [name, value] of Object.entries(created ?? {})) {
      if (name.startsWith('can_')) expect(value, name).toBe(name === 'can_read_status');
    }
  });
});
