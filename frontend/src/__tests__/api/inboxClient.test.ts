/**
 * The in-app inbox client — what goes on the wire, not what comes back.
 *
 * The three questions worth pinning: only the filters that are SET are
 * serialised (an empty `printer_id` must not reach the server as `?printer_id=`,
 * which the list, read-all and clear routes all read); read-all carries the same
 * filter query as the list (the two share a filter object, and a divergence
 * would mark rows the user never saw); and the subscriptions PUT distinguishes
 * an explicit list from `null` ("follow the defaults"), which is the difference
 * between "subscribed to nothing" and "not configured".
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { api } from '../../api/client';

describe('inbox client', () => {
  let seen: URL | null = null;
  let body: unknown = null;

  beforeEach(() => {
    seen = null;
    body = null;
    server.use(
      http.get('/api/v1/inbox/', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json({ items: [], unread_count: 0, next_before_id: null });
      }),
      http.post('/api/v1/inbox/read-all', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json({ updated: 2 });
      }),
      http.put('/api/v1/inbox/subscriptions', async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ is_default: body === null, events: [] });
      }),
    );
  });

  it('serialises only the filters that are set', async () => {
    await api.getInbox({ severity: 'error', unread_only: true, before_id: 40, limit: 20 });
    expect(seen?.searchParams.get('severity')).toBe('error');
    expect(seen?.searchParams.get('unread_only')).toBe('true');
    expect(seen?.searchParams.get('before_id')).toBe('40');
    expect(seen?.searchParams.get('limit')).toBe('20');
    expect(seen?.searchParams.has('printer_id')).toBe(false);
  });

  it('read-all carries the same filters', async () => {
    const r = await api.markInboxAllRead({ printer_id: 3 });
    expect(r.updated).toBe(2);
    expect(seen?.searchParams.get('printer_id')).toBe('3');
  });

  it('subscriptions PUT sends the list or null', async () => {
    await api.updateInboxSubscriptions(['print_failed']);
    expect(body).toEqual({ events: ['print_failed'] });
    await api.updateInboxSubscriptions(null);
    expect(body).toEqual({ events: null });
  });
});
