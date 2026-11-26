/**
 * N07 spec §G (S29, S31, S32): a load that came back from the IdP never autologins,
 * and a terminal refusal of the OIDC attempt ends it.
 */
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { LoginPage } from '../../pages/LoginPage';

const { navigate, loginWithToken } = vi.hoisted(() => ({ navigate: vi.fn(), loginWithToken: vi.fn() }));

vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: () => ({ login: vi.fn(), loginWithToken, requiresSetup: false, user: null, loading: false }),
}));
vi.mock('react-router', async () => ({
  ...(await vi.importActual<typeof import('react-router')>('react-router')),
  useNavigate: () => navigate,
}));

const providerId = 17;

function serve({ statusDelayMs = 0 } = {}) {
  const authorize = vi.fn();
  server.use(
    http.get('/api/v1/auth/advanced-auth/status', async () => {
      if (statusDelayMs) await new Promise((done) => setTimeout(done, statusDelayMs));
      return HttpResponse.json({ advanced_auth_enabled: true, local_login_enabled: true, autologin_provider_id: providerId });
    }),
    http.get('/api/v1/auth/oidc/providers', () =>
      HttpResponse.json([{ id: providerId, name: 'Farm SSO', has_icon: false, is_autologin: true }]),
    ),
    http.get(`/api/v1/auth/oidc/authorize/${providerId}`, () => {
      authorize();
      return HttpResponse.json({ auth_url: '#idp' });
    }),
    http.post('/api/v1/auth/oidc/exchange', () =>
      HttpResponse.json({ requires_2fa: true, pre_auth_token: 'pre-auth', two_fa_methods: ['totp'], user: null }),
    ),
  );
  return authorize;
}

describe('LoginPage after an OIDC return', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it('starts no autologin during 2FA, after the URL is cleaned, and after a terminal refusal', async () => {
    window.history.replaceState(null, '', '/login#oidc_token=exchange-token');
    const authorize = serve({ statusDelayMs: 50 });
    server.use(
      http.post('/api/v1/auth/2fa/verify', () =>
        HttpResponse.json(
          { detail: { error: 'oidc_policy_changed', message: 'The sign-in provider changed during this sign-in.' } },
          { status: 409 },
        ),
      ),
    );
    const view = render(<LoginPage />);
    const code = await screen.findByPlaceholderText(/000000/);
    window.history.replaceState(null, '', '/login');  // the page cleans the fragment
    view.rerender(<LoginPage />);
    await new Promise((done) => setTimeout(done, 100));
    expect(authorize).not.toHaveBeenCalled();

    fireEvent.change(code, { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: /Verify/i }));
    await waitFor(() => expect(screen.queryByPlaceholderText(/000000/)).not.toBeInTheDocument());
    expect(await screen.findByText(/The sign-in provider changed/)).toBeInTheDocument();
    await new Promise((done) => setTimeout(done, 100));
    expect(authorize).not.toHaveBeenCalled();
    // The manual button still starts a new sign-in.
    fireEvent.click(await screen.findByRole('button', { name: /Farm SSO/i }));
    await waitFor(() => expect(authorize).toHaveBeenCalledTimes(1));
  });

  it('shows a callback refusal and starts no autologin', async () => {
    window.history.replaceState(null, '', '/login?oidc_error=group_sync_last_admin');
    window.history.replaceState(null, '', '/login?oidc_error=provider_changed');
    const authorize = serve();
    render(<LoginPage />);
    expect(await screen.findByText(/The sign-in provider changed during sign-in/)).toBeInTheDocument();
    await new Promise((done) => setTimeout(done, 150));
    expect(authorize).not.toHaveBeenCalled();
  });

  it('keeps the 2FA step after an ordinary wrong code', async () => {
    window.history.replaceState(null, '', '/login#oidc_token=exchange-token');
    serve();
    server.use(
      http.post('/api/v1/auth/2fa/verify', () =>
        HttpResponse.json({ detail: 'Invalid verification code' }, { status: 401 }),
      ),
    );
    render(<LoginPage />);
    const code = await screen.findByPlaceholderText(/000000/);
    fireEvent.change(code, { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: /Verify/i }));
    await new Promise((done) => setTimeout(done, 150));
    expect(screen.getByPlaceholderText(/000000/)).toBeInTheDocument();
  });
});

describe('LoginPage — an OIDC return read after mount (A04)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  async function startAutologinThenReturn(returnUrl: string) {
    window.history.replaceState(null, '', '/login');
    let release!: () => void;
    const held = new Promise<void>((done) => { release = done; });
    const authorize = vi.fn();
    server.use(
      http.get('/api/v1/auth/advanced-auth/status', () =>
        HttpResponse.json({ advanced_auth_enabled: true, local_login_enabled: true, autologin_provider_id: providerId }),
      ),
      http.get('/api/v1/auth/oidc/providers', () =>
        HttpResponse.json([{ id: providerId, name: 'Farm SSO', has_icon: false, is_autologin: true }]),
      ),
      http.get(`/api/v1/auth/oidc/authorize/${providerId}`, async () => {
        authorize();
        await held;  // the automatic authorize is still waiting when the return arrives
        return HttpResponse.json({ auth_url: '#auto' });
      }),
      http.post('/api/v1/auth/oidc/exchange', () =>
        HttpResponse.json({ requires_2fa: true, pre_auth_token: 'pre-auth', two_fa_methods: ['totp'], user: null }),
      ),
    );
    render(<LoginPage />);
    await waitFor(() => expect(authorize).toHaveBeenCalledTimes(1));
    window.history.pushState(null, '', returnUrl);  // the same page, no remount
    window.dispatchEvent(new PopStateEvent('popstate'));
    await new Promise((done) => setTimeout(done, 50));
    release();
    await new Promise((done) => setTimeout(done, 150));
    return window.location.hash;
  }

  it('drops a late authorize URL after a callback code', async () => {
    expect(await startAutologinThenReturn('/login?oidc_error=provider_changed')).not.toBe('#auto');
  });

  it('drops a late authorize URL after an exchange fragment', async () => {
    expect(await startAutologinThenReturn('/login#oidc_token=late-exchange')).not.toBe('#auto');
  });
});
