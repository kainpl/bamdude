/**
 * Tests for the Email tab of the notification centre — the four per-user email
 * switches that used to be the whole of `/notifications`.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { EmailDeliveryCard } from '../../../components/notifications/EmailDeliveryCard';

describe('EmailDeliveryCard', () => {
  let saved: unknown = null;
  beforeEach(() => {
    saved = null;
    server.use(
      http.get('/api/v1/user-notifications/preferences', () =>
        HttpResponse.json({ notify_print_start: true, notify_print_complete: true, notify_print_failed: true, notify_print_stopped: true })),
      http.put('/api/v1/user-notifications/preferences', async ({ request }) => {
        const body = await request.json();
        saved = body;
        return HttpResponse.json(body);
      }),
      http.get('/api/v1/auth/me', () =>
        HttpResponse.json({ id: 1, username: 'testuser', email: 'test@example.com', role: 'admin', is_active: true, is_admin: true, groups: [], permissions: [], created_at: '2024-01-01T00:00:00Z' })),
    );
  });

  it('shows a spinner while the preferences are still loading', () => {
    render(<EmailDeliveryCard />);
    expect(document.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('renders the four switches and saves a toggled one', async () => {
    render(<EmailDeliveryCard />);
    const switches = await screen.findAllByRole('switch');
    expect(switches).toHaveLength(4);
    await userEvent.click(switches[0]);
    await userEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(saved).toEqual({
      notify_print_start: false, notify_print_complete: true, notify_print_failed: true, notify_print_stopped: true,
    }));
  });

  it('names the four print events and reflects a toggle in the switch state', async () => {
    render(<EmailDeliveryCard />);
    expect(await screen.findByText('Print Job Starts')).toBeInTheDocument();
    expect(screen.getByText('Print Job Finishes')).toBeInTheDocument();
    expect(screen.getByText('Print Errors')).toBeInTheDocument();
    expect(screen.getByText('Print Job Stops')).toBeInTheDocument();

    const switches = screen.getAllByRole('switch');
    expect(switches[0]).toHaveAttribute('aria-checked', 'true');
    await userEvent.click(switches[0]);
    expect(switches[0]).toHaveAttribute('aria-checked', 'false');
  });
});
