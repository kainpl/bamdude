/**
 * Tests for the AuthContext permission helpers.
 *
 * The opt-in "auth disabled" mode was removed - the system always requires
 * authentication. Tests for the old behavior (everyone is admin, all
 * permissions granted) were deleted.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { AuthProvider, useAuth } from '../../contexts/AuthContext';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { getAuthToken, setAuthToken } from '../../api/client';
import type { Permission } from '../../api/client';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <ThemeProvider>
            <ToastProvider>
              <AuthProvider>{children}</AuthProvider>
            </ToastProvider>
          </ThemeProvider>
        </BrowserRouter>
      </QueryClientProvider>
    );
  };
}

describe('AuthContext', () => {
  describe('when setup is required (no admin yet)', () => {
    beforeEach(() => {
      localStorage.removeItem('auth_token');
      server.use(
        http.get('/api/v1/auth/status', () => {
          return HttpResponse.json({
            auth_enabled: true,
            requires_setup: true,
          });
        }),
      );
    });

    it('requiresSetup is true and user is null', async () => {
      const { result } = renderHook(() => useAuth(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      expect(result.current.requiresSetup).toBe(true);
      expect(result.current.user).toBeNull();
    });
  });

  describe('when auth is required but user is not logged in', () => {
    beforeEach(() => {
      localStorage.removeItem('auth_token');
      server.use(
        http.get('/api/v1/auth/status', () => {
          return HttpResponse.json({
            auth_enabled: true,
            requires_setup: false,
          });
        }),
      );
    });

    it('user is null', async () => {
      const { result } = renderHook(() => useAuth(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      expect(result.current.user).toBeNull();
      expect(result.current.authEnabled).toBe(true);
    });

    it('hasPermission returns false without a user', async () => {
      const { result } = renderHook(() => useAuth(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      expect(result.current.hasPermission('printers:read' as Permission)).toBe(false);
      expect(result.current.hasAnyPermission('printers:read' as Permission)).toBe(false);
      expect(result.current.hasAllPermissions('printers:read' as Permission)).toBe(false);
      expect(result.current.isAdmin).toBe(false);
    });

    it('canModify returns false without a user', async () => {
      const { result } = renderHook(() => useAuth(), {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });

      expect(result.current.canModify('queue', 'update', 1)).toBe(false);
      expect(result.current.canModify('archives', 'delete', null)).toBe(false);
    });
  });

  describe('token validation on mount (#1889)', () => {
    beforeEach(() => {
      // Persisted token lives in jsdom's real localStorage.
      setAuthToken('valid-token');
      server.use(
        http.get('*/api/v1/auth/status', () =>
          HttpResponse.json({ auth_enabled: true, requires_setup: false }),
        ),
      );
    });

    afterEach(() => {
      setAuthToken(null);
      localStorage.removeItem('auth_token');
    });

    it('keeps the stored token when /auth/me fails transiently (does not force re-login)', async () => {
      // Backend not ready yet / brief blip → 500 on every attempt.
      server.use(http.get('*/api/v1/auth/me', () => new HttpResponse(null, { status: 500 })));

      const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });

      await waitFor(() => expect(result.current.loading).toBe(false), { timeout: 4000 });

      // No user this load, but the token MUST survive so a reload can recover —
      // the pre-#1889 blanket catch deleted it, making the session unrecoverable.
      expect(result.current.user).toBeNull();
      expect(getAuthToken()).toBe('valid-token');
      expect(localStorage.getItem('auth_token')).toBe('valid-token');
    });

    it('clears the token on a definitive 401 invalid-token response', async () => {
      server.use(
        http.get('*/api/v1/auth/me', () =>
          HttpResponse.json({ detail: 'Could not validate credentials' }, { status: 401 }),
        ),
      );

      const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });

      await waitFor(() => expect(result.current.loading).toBe(false), { timeout: 4000 });

      expect(result.current.user).toBeNull();
      // Definitive invalid-token → token cleared from memory + localStorage.
      expect(getAuthToken()).toBeNull();
      expect(localStorage.getItem('auth_token')).toBeNull();
    });

    it('loads the user when the stored token is valid', async () => {
      server.use(
        http.get('*/api/v1/auth/me', () =>
          HttpResponse.json({
            id: 1,
            username: 'alice',
            is_active: true,
            permissions: [],
            groups: [],
          }),
        ),
      );

      const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });

      await waitFor(() => expect(result.current.user).not.toBeNull(), { timeout: 4000 });
      expect(result.current.user?.username).toBe('alice');
      expect(getAuthToken()).toBe('valid-token');
    });
  });
});
