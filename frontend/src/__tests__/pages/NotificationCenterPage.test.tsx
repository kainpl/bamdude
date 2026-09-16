/**
 * Tests for the notification centre page: which tabs are offered, and how the
 * `?tab=` query parameter drives the selection.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
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

  it('keeps the heading up and draws no tabs until the gate has answered', () => {
    mockApi({ advanced: true, userNotifications: true });
    render(<NotificationCenterPage />);
    // Synchronous: nothing has resolved yet. The title is already there, and
    // the strip is not — a tab body mounted now would fire its own requests
    // and then be swapped out.
    expect(screen.getByRole('heading', { level: 1, name: 'Notifications' })).toBeInTheDocument();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
  });

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

    // One tab stop, and the panel says which tab it belongs to.
    const selected = screen.getByRole('tab', { name: 'Subscriptions' });
    expect(selected).toHaveAttribute('tabindex', '0');
    expect(selected).toHaveAttribute('aria-controls', 'notification-panel-subscriptions');
    expect(screen.getByRole('tab', { name: 'Inbox' })).toHaveAttribute('tabindex', '-1');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('id', 'notification-panel-subscriptions');
  });

  it('falls back to the inbox when the URL names a tab that is not offered', async () => {
    mockApi({ advanced: false, userNotifications: false });
    window.history.replaceState({}, '', '/notifications?tab=email');
    render(<NotificationCenterPage />);
    // The strip exists only once BOTH gate queries have answered (the test
    // above pins that settled shape in the enabled case), so waiting for it is
    // waiting past the moment the Email tab would have appeared. Asserting
    // before that point would pass on an unanswered gate whatever the code did.
    const strip = await screen.findByRole('tablist');
    expect(within(strip).getAllByRole('tab')).toHaveLength(2);
    expect(within(strip).getByRole('tab', { name: 'Inbox' })).toHaveAttribute('aria-selected', 'true');
    expect(within(strip).queryByRole('tab', { name: 'Email' })).not.toBeInTheDocument();
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'notification-tab-inbox');
  });
});
