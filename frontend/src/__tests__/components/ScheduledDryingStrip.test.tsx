import { describe, it, expect } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
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

const rule = {
  id: 5, printer_id: 1, ams_id: 0, temp: 55, duration_hours: 8, filament: '', rotate_tray: false,
  start_time: '01:00', weekdays: 127, latest_start: null, enabled: true, created_at: null, updated_at: null,
};

function asViewer() {
  server.use(
    http.get('/api/v1/auth/me', () =>
      HttpResponse.json({
        id: 2, username: 'viewer', role: 'user', is_active: true, is_admin: false,
        groups: [{ id: 3, name: 'Viewers' }], permissions: ['printers:read'], created_at: '2024-01-01T00:00:00Z',
      })),
  );
}

describe('ScheduledDryingStrip — who may change what', () => {
  it('a viewer sees what is planned but no controls', async () => {
    asViewer();
    mock([run({ reason: 'printer_busy' })], [rule]);
    render(<ScheduledDryingStrip printerId={1} />);
    expect(await screen.findByText(/the printer is printing/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: /cancel scheduled drying/i })).toBeNull());
    expect(screen.queryByRole('button', { name: /delete schedule/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /pause schedule/i })).toBeNull();
  });

  it('deleting a schedule asks first', async () => {
    const deleted: string[] = [];
    mock([], [rule]);
    server.use(
      http.delete('/api/v1/drying-schedules/:id', ({ params }) => {
        deleted.push(String(params.id));
        return HttpResponse.json({ status: 'deleted', id: Number(params.id) });
      }),
    );
    render(<ScheduledDryingStrip printerId={1} />);
    fireEvent.click(await screen.findByRole('button', { name: /delete schedule/i }));
    const dialog = await screen.findByRole('dialog');
    expect(deleted).toEqual([]);
    fireEvent.click(within(dialog).getByRole('button', { name: /delete schedule/i }));
    await waitFor(() => expect(deleted).toEqual(['5']));
  });

  it('stopping a running scheduled cycle asks first', async () => {
    const deleted = mock([run({ status: 'running', started_at: '2026-09-25T22:00:00Z' })]);
    render(<ScheduledDryingStrip printerId={1} />);
    fireEvent.click(await screen.findByRole('button', { name: /cancel scheduled drying/i }));
    const dialog = await screen.findByRole('dialog');
    expect(deleted).toEqual([]);
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel scheduled drying/i }));
    await waitFor(() => expect(deleted).toEqual(['1']));
  });
});
