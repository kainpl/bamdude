/**
 * Tests for the AuthContext permission helpers.
 *
 * The opt-in "auth disabled" mode was removed — the system always requires
 * authentication. Tests for the old behavior (everyone is admin, all
 * permissions granted) were deleted.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { AuthProvider, useAuth } from '../../contexts/AuthContext';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
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
});
