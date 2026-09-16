/**
 * Tests for the notification centre page: which tabs are offered, and how the
 * `?tab=` query parameter drives the selection.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { NotificationCenterPage } from '../../pages/NotificationCenterPage';

function mockApi({ advanced, userNotifications }: { advanced: boolean; userNotifications: boolean }) {
  server.use(
    http.get('/api/v1/auth/advanced-auth/status', () => HttpResponse.json({ advanced_auth_enabled: advanced, smtp_configured: true })),
    http.get('/api/v1/settings/', () => HttpResponse.json({ user_notifications_enabled: userNotifications })),
    http.get('/api/v1/inbox/', () => HttpResponse.json({ items: [], unread_count: 0, next_before_id: null })),
    http.get('/api/v1/inbox/subscriptions', () => HttpResponse.json({ is_default: true, events: [] })),
    http.get('/api/v1/printers/', () => HttpResponse.json([])),
    http.get('/api/v1/user-notifications/preferences', () =>
      HttpResponse.json({ notify_print_start: true, notify_print_complete: true, notify_print_failed: true, notify_print_stopped: true })),
  );
}

describe('NotificationCenterPage', () => {
  beforeEach(() => window.history.replaceState({}, '', '/notifications'));

  it('shows Inbox and Subscriptions, and Email only under advanced auth', async () => {
    mockApi({ advanced: false, userNotifications: true });
    render(<NotificationCenterPage />);
    expect(await screen.findByRole('tab', { name: 'Inbox' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Subscriptions' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('tab', { name: 'Email' })).not.toBeInTheDocument());
  });

  it('offers the Email tab when advanced auth and user notifications are on', async () => {
    mockApi({ advanced: true, userNotifications: true });
    render(<NotificationCenterPage />);
    const email = await screen.findByRole('tab', { name: 'Email' });
    await userEvent.click(email);
    expect(await screen.findByText('Email Notifications')).toBeInTheDocument();
    expect(window.location.search).toBe('?tab=email');
  });

  it('opens the tab named in the URL', async () => {
    mockApi({ advanced: false, userNotifications: false });
    window.history.replaceState({}, '', '/notifications?tab=subscriptions');
    render(<NotificationCenterPage />);
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Subscriptions' })).toHaveAttribute('aria-selected', 'true'));
  });

  it('falls back to the inbox when the URL names a tab that is not offered', async () => {
    mockApi({ advanced: false, userNotifications: false });
    window.history.replaceState({}, '', '/notifications?tab=email');
    render(<NotificationCenterPage />);
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Inbox' })).toHaveAttribute('aria-selected', 'true'));
    expect(screen.queryByRole('tab', { name: 'Email' })).not.toBeInTheDocument();
  });
});
