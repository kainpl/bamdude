import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { SubscriptionsTab } from '../../../components/notifications/SubscriptionsTab';

const events = [
  { event_type: 'print_failed', severity: 'error', group: 'print', subscribed: true },
  { event_type: 'print_complete', severity: 'info', group: 'print', subscribed: false },
  { event_type: 'sensor_silent', severity: 'warning', group: 'sensors', subscribed: true },
];

describe('SubscriptionsTab', () => {
  const puts: unknown[] = [];

  beforeEach(() => {
    puts.length = 0;
    server.use(
      http.get('/api/v1/inbox/subscriptions', () => HttpResponse.json({ is_default: true, events })),
      http.put('/api/v1/inbox/subscriptions', async ({ request }) => {
        const body = (await request.json()) as { events: string[] | null };
        puts.push(body);
        const active = body.events === null ? ['print_failed', 'sensor_silent'] : body.events;
        return HttpResponse.json({
          is_default: body.events === null,
          events: events.map((e) => ({ ...e, subscribed: active.includes(e.event_type) })),
        });
      }),
    );
  });

  it('groups the catalog and shows the defaults state', async () => {
    render(<SubscriptionsTab />);
    expect(await screen.findByText('Print jobs')).toBeInTheDocument();
    expect(screen.getByText('Sensors')).toBeInTheDocument();
    expect(screen.getByText('Using the defaults: warnings and errors')).toBeInTheDocument();
    expect(screen.getByLabelText('Print failed')).toBeChecked();
    expect(screen.getByLabelText('Print completed')).not.toBeChecked();
  });

  it('a toggle sends the full explicit list', async () => {
    render(<SubscriptionsTab />);
    await userEvent.click(await screen.findByLabelText('Print completed'));
    await waitFor(() => expect(puts).toEqual([{ events: ['print_failed', 'sensor_silent', 'print_complete'] }]));
    expect(await screen.findByText('Custom selection')).toBeInTheDocument();
  });

  it('reset sends null', async () => {
    render(<SubscriptionsTab />);
    await userEvent.click(await screen.findByLabelText('Print completed'));
    // Reset is disabled while the toggle's PUT is in flight, and a click on a
    // disabled button is a silent no-op — so wait for the first save to land.
    await waitFor(() => expect(puts).toHaveLength(1));
    await userEvent.click(await screen.findByRole('button', { name: 'Reset to defaults' }));
    await waitFor(() => expect(puts.at(-1)).toEqual({ events: null }));
  });
});
