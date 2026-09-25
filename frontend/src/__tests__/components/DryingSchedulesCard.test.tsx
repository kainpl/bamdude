import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { DryingSchedulesCard } from '../../components/settings/DryingSchedulesCard';

const rule = {
  id: 5, printer_id: 1, ams_id: 0, temp: 55, duration_hours: 8, filament: '', rotate_tray: false,
  start_time: '01:00', weekdays: 127, latest_start: null, enabled: true, created_at: null, updated_at: null,
};

describe('DryingSchedulesCard', () => {
  it('lists every rule with its printer and edits one through the modal', async () => {
    const patches: unknown[] = [];
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'Farm X1C', model: 'X1C' }])),
      http.get('/api/v1/drying-schedules', () => HttpResponse.json({ server_timezone: 'Europe/Kyiv', schedules: [rule] })),
      http.get('/api/v1/scheduled-dryings', () => HttpResponse.json([])),
      http.patch('/api/v1/drying-schedules/:id', async ({ request, params }) => {
        patches.push({ id: params.id, body: await request.json() });
        return HttpResponse.json({ ...rule, start_time: '02:00' });
      }),
    );
    render(<DryingSchedulesCard />);
    expect(await screen.findByText('Farm X1C')).toBeInTheDocument();
    expect(screen.getByText(/01:00/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /edit schedule/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/start time/i), { target: { value: '02:00' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /save/i }));
    await waitFor(() => expect(patches).toEqual([{ id: '5', body: { start_time: '02:00' } }]));
  });

  it('says so when there are none', async () => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/drying-schedules', () => HttpResponse.json({ server_timezone: 'UTC', schedules: [] })),
    );
    render(<DryingSchedulesCard />);
    expect(await screen.findByText(/no drying schedules yet/i)).toBeInTheDocument();
  });
});
