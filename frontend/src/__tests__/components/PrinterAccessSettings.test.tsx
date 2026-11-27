/**
 * Settings → Users → Printer access (N62): a group limited to a location reaches the printers
 * standing in it or below it, picked printers too; the page saves what changed.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor, within, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrinterAccessSettings } from '../../components/PrinterAccessSettings';

const group = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  description: null,
  permissions: [],
  is_system: false,
  user_count: 2,
  restrict_printers: false,
  printer_ids: [],
  location_ids: [],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...extra,
});

const printer = (id: number, name: string, location_id: number | null) => ({
  id,
  name,
  model: 'X1C',
  location_id,
  location: null,
});

function serve(patches: Array<{ id: string; body: unknown }>) {
  server.use(
    http.get('/api/v1/groups/', () =>
      HttpResponse.json([
        group(1, 'Administrators', { is_system: true }),
        group(2, 'Team A', { restrict_printers: true, location_ids: [10] }),
        group(3, 'Operators'),
      ]),
    ),
    http.get('/api/v1/printers/', () =>
      HttpResponse.json([printer(1, 'Printer A', 11), printer(2, 'Printer B', 20), printer(3, 'Printer C', null)]),
    ),
    http.get('/api/v1/printer-locations', () =>
      HttpResponse.json({
        locations: [
          { id: 10, name: 'Hall', parent_id: null, path: 'Hall', depth: 1, printer_count: 0, sensor_count: 0, queued_count: 0 },
          { id: 11, name: 'Rack', parent_id: 10, path: 'Hall / Rack', depth: 2, printer_count: 1, sensor_count: 0, queued_count: 0 },
          { id: 20, name: 'Lab', parent_id: null, path: 'Lab', depth: 1, printer_count: 1, sensor_count: 0, queued_count: 0 },
        ],
      }),
    ),
    http.patch('/api/v1/groups/:id', async ({ params, request }) => {
      patches.push({ id: String(params.id), body: await request.json() });
      return HttpResponse.json(group(Number(params.id), 'Team A'));
    }),
  );
}

describe('PrinterAccessSettings', () => {
  it('never offers Administrators and shows a limited group reaching a printer below its location', async () => {
    serve([]);
    render(<PrinterAccessSettings />);

    const team = await screen.findByTestId('access-group-2');
    expect(screen.queryByTestId('access-group-1')).not.toBeInTheDocument();
    // Printer A stands on the rack below the hall: reached, and not unpickable on its own.
    const a = within(team).getByLabelText(/Printer A/);
    expect(a).toBeChecked();
    expect(a).toBeDisabled();
    expect(within(team).getByLabelText(/Printer B/)).not.toBeChecked();
  });

  it('saves a picked printer for the group that changed, and nothing else', async () => {
    const patches: Array<{ id: string; body: unknown }> = [];
    serve(patches);
    render(<PrinterAccessSettings />);

    const team = await screen.findByTestId('access-group-2');
    fireEvent.click(within(team).getByLabelText(/Printer C/));
    fireEvent.click(screen.getByRole('button', { name: 'Save 1 group' }));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ id: '2', body: { restrict_printers: true, printer_ids: [3], location_ids: [10] } });
  });

  it('opens on a printer and says which groups reach it', async () => {
    serve([]);
    render(<PrinterAccessSettings initialPrinterId={2} />);

    const panel = await screen.findByTestId('access-printer-panel');
    // Operators is not limited: it reaches every printer, so its box is ticked and fixed.
    const operators = within(panel).getByLabelText(/Operators/);
    expect(operators).toBeChecked();
    expect(operators).toBeDisabled();
    // Team A reaches only the hall: printer B (in the lab) is not theirs until picked.
    expect(within(panel).getByLabelText(/Team A/)).not.toBeChecked();
  });
});
