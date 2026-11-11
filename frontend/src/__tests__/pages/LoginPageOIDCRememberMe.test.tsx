import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { LoginPage } from '../../pages/LoginPage';

const { navigate, loginWithToken } = vi.hoisted(() => ({
  navigate: vi.fn(),
  loginWithToken: vi.fn(),
}));

// Exercise the real API client and query/mutation lifecycle as a logged-out
// visitor; the shared render helper otherwise seeds an authenticated user.
vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: () => ({
    login: vi.fn(), loginWithToken, requiresSetup: false, user: null, loading: false,
  }),
}));
vi.mock('react-router', async () => ({
  ...await vi.importActual<typeof import('react-router')>('react-router'),
  useNavigate: () => navigate,
}));

const providerId = 17;
const redirectKey = 'auth_post_login_redirect';
const ssoStatus = { advanced_auth_enabled: true, local_login_enabled: false, autologin_provider_id: null };

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('LoginPage OIDC Remember Me', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    window.history.replaceState(null, '', '/login');
    server.use(
      http.get('/api/v1/auth/advanced-auth/status', () => HttpResponse.json(ssoStatus)),
      http.get('/api/v1/auth/oidc/providers', () => HttpResponse.json([
        { id: providerId, name: 'Farm SSO', has_icon: false, is_autologin: false },
      ])),
    );
  });

  async function ssoControls() {
    await waitFor(() => expect(screen.queryByLabelText(/Username/i)).not.toBeInTheDocument());
    const checkbox = await screen.findByRole('checkbox', { name: /Remember me/i });
    const button = await screen.findByRole('button', { name: /Sign in with Farm SSO/i });
    return { checkbox, button };
  }

  it('shows an unchecked Remember Me choice when local login is disabled and resets it on a fresh mount', async () => {
    const first = render(<LoginPage />);
    const { checkbox } = await ssoControls();
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    expect(checkbox).toBeChecked();
    first.unmount();

    render(<LoginPage />);
    expect((await ssoControls()).checkbox).not.toBeChecked();
  });

  it.each([false, true])('captures manual remember_me=%s for this request even if the checkbox changes while pending', async (remember) => {
    const gate = deferred();
    const choices: (string | null)[] = [];
    server.use(http.get(`/api/v1/auth/oidc/authorize/${providerId}`, async ({ request }) => {
      choices.push(new URL(request.url).searchParams.get('remember_me'));
      await gate.promise;
      // Hash-only navigation is supported by jsdom and stands in for the IdP.
      return HttpResponse.json({ auth_url: '#fake-idp' });
    }));
    render(<LoginPage />);
    const { checkbox, button } = await ssoControls();
    if (remember) fireEvent.click(checkbox);
    fireEvent.click(button);
    fireEvent.click(checkbox);
    try {
      await waitFor(() => expect(choices).toEqual([String(remember)]));
      expect(checkbox).toHaveProperty('checked', !remember);
    } finally {
      gate.resolve();
    }
    await waitFor(() => expect(window.location.hash).toBe('#fake-idp'));
  });

  it('uses explicit false for autologin even if a stale checked choice exists before status loads', async () => {
    const statusGate = deferred();
    const choices: (string | null)[] = [];
    server.use(
      http.get('/api/v1/auth/advanced-auth/status', async () => {
        await statusGate.promise;
        return HttpResponse.json({ ...ssoStatus, autologin_provider_id: providerId });
      }),
      http.get(`/api/v1/auth/oidc/authorize/${providerId}`, ({ request }) => {
        choices.push(new URL(request.url).searchParams.get('remember_me'));
        return HttpResponse.json({ auth_url: '#auto-idp' });
      }),
    );
    render(<LoginPage />);
    const checkbox = await screen.findByRole('checkbox', { name: /Remember me/i });
    fireEvent.click(checkbox);
    expect(checkbox).toBeChecked();
    statusGate.resolve();
    await waitFor(() => expect(choices).toEqual(['false']));
    await waitFor(() => expect(window.location.hash).toBe('#auto-idp'));
  });

  it('retains a safe router deep link through manual SSO authorization', async () => {
    window.history.replaceState({ usr: { from: { pathname: '/printers/7', search: '?panel=camera' } } }, '', '/login');
    server.use(http.get(`/api/v1/auth/oidc/authorize/${providerId}`, () => HttpResponse.json({ auth_url: '#deep-idp' })));
    render(<LoginPage />);
    fireEvent.click((await ssoControls()).button);
    await waitFor(() => expect(sessionStorage.getItem(redirectKey)).toBe('/printers/7?panel=camera'));
    await waitFor(() => expect(window.location.hash).toBe('#deep-idp'));
  });

  it.each([
    ['/printers/7?panel=camera', '/printers/7?panel=camera'],
    ['https://outside.invalid/', '/'],
    ['//outside.invalid/', '/'],
    ['/login?again=1', '/'],
  ])('consumes callback redirect %s safely as %s', async (saved, expected) => {
    sessionStorage.setItem(redirectKey, saved);
    window.history.replaceState(null, '', '/login#oidc_token=synthetic-one-time-token');
    const user = { id: 7, username: 'sso-user', auth_source: 'oidc', is_active: true };
    server.use(http.post('/api/v1/auth/oidc/exchange', () => HttpResponse.json({ access_token: 'synthetic-access-token', user })));
    render(<LoginPage />);
    await waitFor(() => expect(loginWithToken).toHaveBeenCalledWith('synthetic-access-token', user));
    expect(navigate).toHaveBeenCalledWith(expected, { replace: true });
    expect(sessionStorage.getItem(redirectKey)).toBeNull();
  });
});
