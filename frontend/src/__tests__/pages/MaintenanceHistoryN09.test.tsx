/**
 * N09 (upstream 8afcb3fbd): a reset asks first and takes a comment; the History tab shows the
 * names kept at each reset, deletes one entry or clears by age with maintenance:delete, and
 * exports CSV beside Excel.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render as renderBare, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { MaintenancePage } from '../../pages/MaintenancePage';
import { PerformMaintenanceModal } from '../../components/Maintenance/PerformMaintenanceModal';
import { ClearHistoryModal } from '../../components/Maintenance/ClearHistoryModal';

describe('the reset dialog', () => {
  it('sends the trimmed comment, and none when it is blank', async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    const { unmount } = renderBare(
      <PerformMaintenanceModal taskName="Clean Nozzle" printerName="X1" onConfirm={onConfirm} onCancel={vi.fn()} />,
    );
    await user.type(screen.getByRole('textbox'), '  greased  ');
    await user.click(screen.getByRole('button', { name: 'Reset' }));
    expect(onConfirm).toHaveBeenLastCalledWith('greased');
    unmount();

    renderBare(<PerformMaintenanceModal taskName="Clean Nozzle" printerName="X1" onConfirm={onConfirm} onCancel={vi.fn()} />);
    await user.type(screen.getByRole('textbox'), '   ');
    await user.click(screen.getByRole('button', { name: 'Reset' }));
    expect(onConfirm).toHaveBeenLastCalledWith(undefined);
  });

  it('Esc closes the empty dialog and keeps one with a typed comment', async () => {
    const onCancel = vi.fn();
    const user = userEvent.setup();
    renderBare(<PerformMaintenanceModal taskName="Clean Nozzle" printerName="X1" onConfirm={vi.fn()} onCancel={onCancel} />);

    await user.type(screen.getByRole('textbox'), 'half a note');
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).not.toHaveBeenCalled();

    await user.clear(screen.getByRole('textbox'));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('caps the comment at the server limit', () => {
    renderBare(<PerformMaintenanceModal taskName="T" printerName="P" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole('textbox')).toHaveAttribute('maxLength', '10000');
  });
});

describe('the clear dialog', () => {
  it('takes a whole number of days from 1 and names its scope', async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    renderBare(<ClearHistoryModal printerName="X1" onConfirm={onConfirm} onCancel={vi.fn()} />);
    expect(screen.getByText('Only the entries of X1.')).toBeInTheDocument();

    const days = screen.getByRole('spinbutton');
    await user.clear(days);
    await user.type(days, '0');
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();

    await user.clear(days);
    await user.type(days, '30');
    await user.click(screen.getByRole('button', { name: 'Delete' }));
    expect(onConfirm).toHaveBeenCalledWith(30);
  });
});

describe('the maintenance page', () => {
  let posted: { url: string; method: string; body?: unknown }[];
  const { createObjectURL, revokeObjectURL } = URL;

  beforeEach(() => {
    posted = [];
    // jsdom has no object URLs; the export hands its file to one.
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:export') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'X1 Carbon', model: 'X1C' }])),
      http.get('/api/v1/maintenance/types', () => HttpResponse.json([])),
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json([
          {
            printer_id: 1,
            printer_name: 'X1 Carbon',
            due_count: 0,
            warning_count: 0,
            total_print_hours: 100,
            maintenance_items: [
              {
                id: 7,
                printer_id: 1,
                printer_name: 'X1 Carbon',
                maintenance_type_id: 1,
                maintenance_type_name: 'Clean Nozzle',
                enabled: true,
                interval_hours: 50,
                interval_type: 'hours',
                hours_since_maintenance: 10,
                hours_until_due: 40,
                is_due: false,
                is_warning: false,
              },
            ],
          },
        ]),
      ),
      http.post('/api/v1/maintenance/items/:id/perform', async ({ request, params }) => {
        posted.push({ url: `perform ${params.id}`, method: 'POST', body: await request.json() });
        return HttpResponse.json({ id: Number(params.id) });
      }),
      http.get('/api/v1/maintenance/history', () =>
        HttpResponse.json({
          items: [
            {
              id: 5,
              printer_maintenance_id: null,
              printer_id: null,
              performed_at: '2026-11-01T10:00:00',
              hours_at_maintenance: 12.5,
              notes: 'greased',
              printer_name: 'Sold printer',
              maintenance_type_name: 'Clean Nozzle',
              performed_by_user_id: null,
              performed_by_chat_id: null,
              performed_by_name: 'former-operator',
            },
          ],
          total: 1,
          page: 1,
          per_page: 25,
          last_page: 1,
        }),
      ),
      http.delete('/api/v1/maintenance/history/:id', ({ params }) => {
        posted.push({ url: `delete ${params.id}`, method: 'DELETE' });
        return HttpResponse.json({ deleted: 1 });
      }),
      http.delete('/api/v1/maintenance/history', ({ request }) => {
        posted.push({ url: `clear ${new URL(request.url).search}`, method: 'DELETE' });
        return HttpResponse.json({ deleted: 3 });
      }),
      http.get('/api/v1/maintenance/history/export', ({ request }) => {
        posted.push({ url: `export ${new URL(request.url).search}`, method: 'GET' });
        return new HttpResponse('﻿Date,Printer', { headers: { 'content-type': 'text/csv' } });
      }),
    );
  });

  afterEach(() => {
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
  });

  it('asks before a reset and sends the comment', async () => {
    const user = userEvent.setup();
    render(<MaintenancePage />);

    await user.click(await screen.findByRole('button', { name: 'Expand' }));
    await user.click(await screen.findByRole('button', { name: 'Reset' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Reset "Clean Nozzle" on X1 Carbon\?/)).toBeInTheDocument();
    expect(posted).toEqual([]);

    await user.type(within(dialog).getByRole('textbox'), 'new nozzle wiper');
    await user.click(within(dialog).getByRole('button', { name: 'Reset' }));

    await waitFor(() => expect(posted).toEqual([{ url: 'perform 7', method: 'POST', body: { notes: 'new nozzle wiper' } }]));
  });

  it('cancelling a reset sends nothing', async () => {
    const user = userEvent.setup();
    render(<MaintenancePage />);

    await user.click(await screen.findByRole('button', { name: 'Expand' }));
    await user.click(await screen.findByRole('button', { name: 'Reset' }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(posted).toEqual([]);
  });

  async function openHistory(user: ReturnType<typeof userEvent.setup>) {
    render(<MaintenancePage />);
    await user.click(await screen.findByRole('button', { name: /^History$/ }));
    return screen.findByText('Sold printer');
  }

  it('shows the names kept at the reset, also of a deleted printer and user', async () => {
    const user = userEvent.setup();
    await openHistory(user);

    expect(screen.getByText('former-operator')).toBeInTheDocument();
  });

  it('deletes one entry only after it is confirmed', async () => {
    const user = userEvent.setup();
    await openHistory(user);

    await user.click(screen.getByRole('button', { name: 'Delete entry' }));
    const dialog = await screen.findByRole('dialog');
    expect(posted).toEqual([]);
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(posted).toEqual([{ url: 'delete 5', method: 'DELETE' }]));
  });

  it('clears entries older than N days', async () => {
    const user = userEvent.setup();
    await openHistory(user);

    await user.click(screen.getByRole('button', { name: /Clear/ }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('The entries of every printer you can see.')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(posted).toEqual([{ url: 'clear ?older_than_days=90', method: 'DELETE' }]));
  });

  it('exports CSV beside Excel', async () => {
    const user = userEvent.setup();
    await openHistory(user);

    await user.click(screen.getByRole('button', { name: 'CSV' }));

    await waitFor(() => expect(posted).toEqual([{ url: 'export ?format=csv', method: 'GET' }]));
  });
});
