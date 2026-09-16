/**
 * The Bell shows the unread count for everyone with an inbox, and is no longer
 * tied to advanced auth or the user-email setting (spec: notification-center §8.1).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { Layout } from '../../components/Layout';

describe('Layout inbox badge', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/version', () => HttpResponse.json({ version: '0.0.0', build: 'test' })),
      http.get('/api/v1/settings/', () => HttpResponse.json({ check_updates: false, user_notifications_enabled: false })),
      http.get('/api/v1/auth/advanced-auth/status', () => HttpResponse.json({ advanced_auth_enabled: false })),
      http.get('/api/v1/external-links/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/support/debug-logging', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/printers/developer-mode-warnings', () => HttpResponse.json([])),
      http.get('/api/v1/auto-queue/', () => HttpResponse.json([])),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/inbox/unread-count', () => HttpResponse.json({ unread_count: 3 })),
    );
  });

  it('shows the Bell with the unread count even without advanced auth', async () => {
    render(<Layout />);
    // One link in the sidebar; a second one appears only in the compact header
    // (matchMedia is stubbed in setup.ts, so normally just the sidebar).
    const links = await screen.findAllByRole('link', { name: /notifications/i });
    expect(links[0]).toHaveAttribute('href', '/notifications');
    await waitFor(() => expect(screen.getAllByText('3').length).toBeGreaterThan(0));
  });
});
