import { describe, it, expect } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ScheduledDryingStrip } from '../../components/ScheduledDryingStrip';

const run = (over: Record<string, unknown>) => ({
  id: 1, printer_id: 1, ams_id: 0, temp: 55, duration_hours: 8, filament: 'PLA', rotate_tray: false,
  schedule_id: null, start_after: '2026-09-25T22:00:00Z', latest_start: null, status: 'pending', reason: null,
  detail: null, created_at: '2026-09-25T10:00:00Z', started_at: null, completed_at: null, ...over,
});

function mock(runs: unknown[], schedules: unknown[] = []) {
  const deleted: string[] = [];
  server.use(
    http.get('/api/v1/scheduled-dryings', () => HttpResponse.json(runs)),
    http.get('/api/v1/drying-schedules', () => HttpResponse.json({ server_timezone: 'Europe/Kyiv', schedules })),
    http.delete('/api/v1/scheduled-dryings/:id', ({ params }) => {
      deleted.push(String(params.id));
      return HttpResponse.json({ status: 'cancelled', id: Number(params.id) });
    }),
  );
  return deleted;
}

describe('ScheduledDryingStrip', () => {
  it('renders nothing when there is nothing to show', async () => {
    mock([]);
    const { container } = render(<ScheduledDryingStrip printerId={1} />);
    await waitFor(() => expect(container.querySelector('[data-testid="scheduled-drying-strip"]')).toBeNull());
  });

  it('shows why a pending run waits and cancels it', async () => {
    const deleted = mock([run({ reason: 'printer_busy' })]);
    render(<ScheduledDryingStrip printerId={1} />);
    expect(await screen.findByText(/the printer is printing/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /cancel scheduled drying/i }));
    await waitFor(() => expect(deleted).toEqual(['1']));
  });

  it('shows a skipped run with its reason', async () => {
    mock([run({ status: 'skipped', reason: 'window_passed' })]);
    render(<ScheduledDryingStrip printerId={1} />);
    expect(await screen.findByText(/start window passed/i)).toBeInTheDocument();
  });

  it('lists a rule', async () => {
    mock([], [{ id: 5, printer_id: 1, ams_id: 0, temp: 55, duration_hours: 8, filament: '', rotate_tray: false,
      start_time: '01:00', weekdays: 127, latest_start: '03:00', enabled: true, created_at: null, updated_at: null }]);
    render(<ScheduledDryingStrip printerId={1} />);
    expect(await screen.findByText(/01:00/)).toBeInTheDocument();
  });
});
