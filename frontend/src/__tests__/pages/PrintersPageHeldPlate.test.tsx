import { describe, it, expect } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const printer = {
  id: 1, name: 'Synthetic Printer', ip_address: '192.0.2.1',
  serial_number: '01P00A000000001', access_code: '12345678', model: 'A1M',
  enabled: true, is_active: true, require_plate_clear: false, plate_detection_enabled: false,
  nozzle_diameter: 0.4, nozzle_type: 'stainless_steel', location: null, auto_archive: true,
  created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
};
const pendingItem = {
  id: 11, printer_id: 1, status: 'pending', manual_start: false,
  archive_id: 500, archive_name: 'Product A.gcode.3mf', position: 1,
  created_at: '2024-01-01T00:00:00Z',
};

function mockHeldPlate(state: string, pending: boolean, awaiting = true, connected = true) {
  const status = {
    connected, state, awaiting_plate_clear: awaiting, repeat_available: true, progress: 100,
    layer_num: 1, total_layers: 1, temperatures: { nozzle: 25, bed: 25, chamber: 25 },
    remaining_time: 0, filename: null, wifi_signal: -50, vt_tray: [],
  };
  const posted: unknown[] = [];
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', () => HttpResponse.json({ 1: status })),
    http.get('/api/v1/queue/', () => HttpResponse.json(pending ? [pendingItem] : [])),
    http.get('/api/v1/printers/:id/waiting-print', () => HttpResponse.json({
      archive_id: 9, gate_token: 'synthetic-gate-9', print_name: 'Product A',
      status: 'completed', quantity: 1, defective_count: 0,
      parts: [{ id: 21, name: 'Part A', name_key: 'part a', quantity: 1, defective: 0 }],
    })),
    http.post('/api/v1/printers/:id/clear-plate', async ({ request }) => {
      posted.push(await request.json());
      status.awaiting_plate_clear = false;
      return HttpResponse.json({ success: true, message: 'Plate cleared', ledger_refused_parts: 0 });
    }),
  );
  return posted;
}

describe('an existing plate hold remains answerable with printer confirmation disabled', () => {
  it.each([
    ['1', 'FINISH', false, true],
    ['2', 'FINISH', false, true],
    ['1', 'FINISH', true, true],
    ['2', 'FINISH', true, true],
    ['2', 'FAILED', true, true],
    ['2', 'IDLE', false, true],
    ['1', 'FINISH', false, false],
    ['2', 'FINISH', false, false],
  ] as const)('size %s, state %s, queue %s, online %s: clears the displayed run once', async (size, state, pending, online) => {
    localStorage.setItem('printerCardSize', size);
    const posted = mockHeldPlate(state, pending, true, online);
    render(<PrintersPage />);
    await screen.findByText('Synthetic Printer');
    await screen.findByTestId('plate-defects-toggle');
    const buttons = screen.getAllByRole('button', { name: /Clear plate/i });
    expect(buttons).toHaveLength(1);
    expect(posted).toHaveLength(0);
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(posted).toEqual([
      { expected_archive_id: 9, expected_gate_token: 'synthetic-gate-9' },
    ]));
    await waitFor(() => expect(screen.queryByRole('button', { name: /Clear plate/i })).not.toBeInTheDocument());
  });

  it.each(['1', '2'])('size %s: shows no clearance button when there is no hold', async (size) => {
    localStorage.setItem('printerCardSize', size);
    mockHeldPlate('FINISH', true, false);
    render(<PrintersPage />);
    await screen.findByText('Synthetic Printer');
    await screen.findByText(size === '1' ? 'Next: Product A.gcode.3mf' : 'Product A.gcode.3mf');
    expect(screen.queryByRole('button', { name: /Clear plate/i })).not.toBeInTheDocument();
  });

  it.each(['RUNNING', 'PAUSE'])('does not offer clearance during %s', async (state) => {
    localStorage.setItem('printerCardSize', '2');
    mockHeldPlate(state, true);
    render(<PrintersPage />);
    await screen.findByText('Synthetic Printer');
    await screen.findByText('Product A.gcode.3mf');
    expect(screen.queryByRole('button', { name: /Clear plate/i })).not.toBeInTheDocument();
  });
});
