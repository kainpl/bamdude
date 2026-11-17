/**
 * The app's own route table around Model sources: the old /makerworld address, and who may enter.
 * The REAL routes are rendered (App.tsx's `AppRoutes`); only the shell around the pages is stubbed.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import '../utils'; // seeds the test token
import { AppRoutes } from '../../App';
import { AuthProvider } from '../../contexts/AuthContext';
import { EmbeddedCameraProvider } from '../../contexts/EmbeddedCameraContext';
import { ManyfoldImportProvider } from '../../contexts/ManyfoldImportContext';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';

// The shell (sidebar, sockets, tab title) is not what is under test.
vi.mock('../../components/Layout', async () => {
  const { Outlet } = await vi.importActual<typeof import('react-router')>('react-router');
  return { Layout: () => <Outlet /> };
});
vi.mock('../../hooks/useWebSocket', () => ({ useWebSocket: () => {} }));
vi.mock('../../hooks/useConnectionToast', () => ({ useConnectionToast: () => {} }));
vi.mock('../../hooks/usePrintProgressTitle', () => ({ usePrintProgressTitle: () => {} }));
vi.mock('../../pages/PrintersPage', () => ({ PrintersPage: () => <p>the printers page</p> }));

function open(address: string) {
  server.use(
    http.get('/api/v1/model-sources/manyfold/status', () =>
      HttpResponse.json({ source: 'manyfold', name: 'Manyfold', configured: true, usable: true, base_url: null }),
    ),
    http.get('/api/v1/model-sources/manyfold/models', () =>
      HttpResponse.json({ total: 0, page: 1, pages: 1, has_next: false, has_previous: false, models: [] }),
    ),
    http.get('/api/v1/makerworld/status', () =>
      HttpResponse.json({ has_cloud_token: true, can_download: true, sign_in_expired: false }),
    ),
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
  );
  const router = createMemoryRouter([{ path: '*', element: <AppRoutes /> }], { initialEntries: [address] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AuthProvider>
          <ToastProvider>
            <EmbeddedCameraProvider>
              <ManyfoldImportProvider>
                <RouterProvider router={router} />
              </ManyfoldImportProvider>
            </EmbeddedCameraProvider>
          </ToastProvider>
        </AuthProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
  return router;
}

function asUser(permissions: string[]) {
  server.use(
    http.get('/api/v1/auth/me', () =>
      HttpResponse.json({
        id: 2,
        username: 'operator',
        role: 'user',
        is_active: true,
        is_admin: false,
        groups: [],
        permissions,
        created_at: '2024-01-01T00:00:00Z',
      }),
    ),
  );
}

describe('the Model sources routes', () => {
  it('an old /makerworld link or bookmark lands on the MakerWorld tab of Model sources', async () => {
    const router = open('/makerworld');

    await waitFor(() => expect(router.state.location.pathname).toBe('/model-sources'));
    expect(router.state.location.search).toBe('?tab=makerworld');
    expect(await screen.findByRole('tab', { name: 'MakerWorld' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Import from MakerWorld')).toBeInTheDocument();
  });

  it('lets a user in with either source right', async () => {
    asUser(['manyfold:view']);
    const router = open('/model-sources');

    expect(await screen.findByText('No models found.')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/model-sources');
  });

  it('sends a user with neither source right away from the page', async () => {
    asUser(['printers:read']);
    const router = open('/model-sources?tab=manyfold');

    expect(await screen.findByText('the printers page')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/');
  });
});
