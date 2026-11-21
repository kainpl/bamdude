/**
 * Test utilities and wrapper components.
 */

import React from 'react';
import { render } from '@testing-library/react';
import type { RenderOptions } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router';
import { ThemeProvider } from '../contexts/ThemeContext';
import { ToastProvider } from '../contexts/ToastContext';
import { AuthProvider } from '../contexts/AuthContext';
import { EmbeddedCameraProvider } from '../contexts/EmbeddedCameraContext';
import { setAuthToken } from '../api/client';

// BamDude has always-on auth (see CLAUDE.md). AuthContext will not load a user
// unless a token is already present when checkAuthStatus runs — otherwise every
// hasPermission call falls through to `permissionSet.has(...)` on an empty set
// and everything that uses `hasPermission(...)` as a gate (most nav items,
// many UI actions) disappears from the DOM. Seed a synthetic token via the
// canonical setter so api/client's module-level cache stays consistent with
// localStorage. Test files that vi.mock '../api/client' need to include
// setAuthToken in their mock factory — see utils.tsx guard below.
if (typeof setAuthToken === 'function') {
  setAuthToken('test-admin-token');
} else if (typeof localStorage !== 'undefined') {
  localStorage.setItem('auth_token', 'test-admin-token');
}

// Create a new QueryClient for each test
function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
      },
      mutations: {
        retry: false,
      },
    },
  });
}

interface AllProvidersProps {
  children: React.ReactNode;
}

function AllProviders({ children }: AllProvidersProps) {
  const queryClient = createTestQueryClient();

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        {/* Tests that exercise server theme synchronization mount their own provider. */}
        <ThemeProvider syncServer={false}>
          <AuthProvider>
            {/* In the app the floating camera window is the shell's (Layout).
                A page rendered on its own gets the same owner here, so its
                camera buttons open the window they open in the app. */}
            <ToastProvider><EmbeddedCameraProvider>{children}</EmbeddedCameraProvider></ToastProvider>
          </AuthProvider>
        </ThemeProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

/**
 * Custom render function that wraps components with all providers.
 */
function customRender(
  ui: React.ReactElement,
  options?: Omit<RenderOptions, 'wrapper'>
) {
  return render(ui, { wrapper: AllProviders, ...options });
}

// Re-export everything from testing-library
export * from '@testing-library/react';

// Override render with our custom render
export { customRender as render };
