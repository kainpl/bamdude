/**
 * An already-authenticated visitor must not be shown the login form (#1889).
 *
 * A live session that lands directly on /login — typically the address bar
 * autocompleting the origin to its most-visited path — used to render the
 * credentials form even though the token was valid and every request was
 * succeeding, which reads to the user as "it never stays logged in".
 *
 * Deliberately its own file. The assertion is on the router's `navigate`,
 * because LoginPage is rendered in isolation here and keeps rendering its own
 * markup regardless of where it tells the router to go — and in the shared
 * LoginPage suite a preceding test's login mutation resolves asynchronously and
 * calls `navigate('/')` during this test's waitFor window, which made the
 * assertion pass against unfixed code. One test per file is the only reliable
 * isolation for a module-scoped spy.
 */

import { describe, it, expect, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { LoginPage } from '../../pages/LoginPage';

const navigateSpy = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => navigateSpy };
});

describe('LoginPage — already-authenticated visitor (#1889)', () => {
  it('navigates away instead of rendering the credentials form', async () => {
    server.use(
      http.get('/api/v1/auth/status', () =>
        HttpResponse.json({ auth_enabled: true, requires_setup: false }),
      ),
    );
    // The shared harness already sets a token and /auth/me returns a user, so
    // this render is the authenticated case.
    render(<LoginPage />);

    await waitFor(() => {
      expect(navigateSpy).toHaveBeenCalledWith('/', { replace: true });
    });
  });
});
