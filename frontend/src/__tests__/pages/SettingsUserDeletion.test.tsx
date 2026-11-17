import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { SettingsPage } from '../../pages/SettingsPage';

describe('bounded user content deletion', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/settings?tab=users');
    server.use(
      http.get('/api/v1/users/', () => HttpResponse.json([{ id: 77, username: 'content-owner', email: '', is_active: true, is_admin: false, role: 'user', groups: [], auth_source: 'local' }])),
      http.get('/api/v1/users/77/items-count', () => HttpResponse.json({ archives: 0, library_files: 0, queue_items: 0, auto_queue_items: 2 })),
    );
  });

  it('offers deletion for an AutoQueue-only account and names order work to replan', async () => {
    let called = false;
    server.use(http.delete('/api/v1/users/77', ({ request }) => {
      expect(new URL(request.url).searchParams.get('delete_items')).toBe('true');
      called = true;
      return HttpResponse.json({ deleted: true, counts: { archives: 0, library_files: 0, queue_items: 0, auto_queue_items: 2 }, affected_orders: [{ id: 8, name: 'Order C', prints: 0, jobs: 2 }] });
    }));
    render(<SettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: /delete user: content-owner/i }));
    expect(await screen.findByText('AutoQueue jobs: 2')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete user AND their items' }));
    expect(await screen.findByText(/Removed print work from orders: Order C/)).toBeInTheDocument();
    await waitFor(() => expect(called).toBe(true));
    expect(screen.queryByRole('dialog', { name: /delete user/i })).not.toBeInTheDocument();
  });

  it('retains the confirmation and explains a physical-work refusal', async () => {
    server.use(http.delete('/api/v1/users/77', () => HttpResponse.json({ detail: { error: 'user_content_delete_active', message: 'Resolve the retained print through recovery, then retry.' } }, { status: 409 })));
    render(<SettingsPage />);
    fireEvent.click(await screen.findByRole('button', { name: /delete user: content-owner/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete user AND their items' }));
    expect(await screen.findByText('Resolve the retained print through recovery, then retry.')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: /delete user/i })).toBeInTheDocument();
  });
});
