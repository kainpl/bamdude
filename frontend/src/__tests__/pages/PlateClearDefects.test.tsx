/**
 * The defects counters beside the printer card's YELLOW pair — the pair the
 * operator sees once the queue has run dry (expanded card) and the only pair
 * the compact card ever draws. The green pair in the queue widget has the same
 * row (PrinterQueueWidgetClearPlate.test.tsx); this file pins that the yellow
 * one is not the place without it, and that the two never double up.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const printer = {
  id: 1,
  name: 'X2D Farm',
  ip_address: '192.168.1.100',
  serial_number: '20P00A000000001',
  access_code: '12345678',
  model: 'X2D',
  enabled: true,
  nozzle_diameter: 0.4,
  nozzle_type: 'hardened_steel',
  location: null,
  auto_archive: true,
  require_plate_clear: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const finishedAwaitingClear = {
  connected: true,
  state: 'FINISH',
  progress: 100,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
  awaiting_plate_clear: true,
};

const pendingItem = {
  id: 11,
  printer_id: 1,
  status: 'pending',
  manual_start: false,
  archive_id: 500,
  archive_name: 'next_job.gcode.3mf',
  position: 1,
  created_at: '2024-01-01T00:00:00Z',
};

const waitingPrint = {
  archive_id: 9,
  print_name: 'Done',
  status: 'completed',
  quantity: 2,
  defective_count: 0,
  parts: [{ id: 21, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 }],
};

function mockApi(pending: unknown[]) {
  const posted: { clear: unknown | null; repeat: unknown | null; clearCalls: number } = {
    clear: null,
    repeat: null,
    clearCalls: 0,
  };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(finishedAwaitingClear)),
    http.get('/api/v1/queue/', () => HttpResponse.json(pending)),
    http.get('/api/v1/printers/:id/waiting-print', () => HttpResponse.json(waitingPrint)),
    http.post('/api/v1/printers/:id/clear-plate', async ({ request }) => {
      const text = await request.text();
      posted.clearCalls += 1;
      posted.clear = text ? JSON.parse(text) : null;
      return HttpResponse.json({ success: true, message: 'Plate cleared', ledger_refused_parts: 0 });
    }),
    http.post('/api/v1/printers/:id/repeat-print', async ({ request }) => {
      const text = await request.text();
      posted.repeat = text ? JSON.parse(text) : null;
      return HttpResponse.json({ success: true, item_id: 11, ledger_refused_parts: 0 });
    }),
  );
  return posted;
}

async function waitForCard() {
  await waitFor(() => {
    expect(screen.getByText('X2D Farm')).toBeInTheDocument();
  });
}

describe('the yellow pair (expanded card, queue run dry)', () => {
  beforeEach(() => localStorage.setItem('printerCardSize', '2'));

  it('offers the defects row above the pair', async () => {
    mockApi([]);
    render(<PrintersPage />);
    await waitForCard();
    expect(await screen.findByTestId('plate-defects-toggle')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Clear plate/i })).toBeInTheDocument();
  });

  it('a touched counter travels in the clear-plate body', async () => {
    const posted = mockApi([]);
    render(<PrintersPage />);
    await waitForCard();
    fireEvent.click(await screen.findByTestId('plate-defects-toggle'));
    const lid = (await screen.findByTestId('part-defective-21')) as HTMLInputElement;
    fireEvent.change(lid, { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: /Clear plate/i }));
    await waitFor(() =>
      expect(posted.clear).toEqual({ defects: { parts: [{ id: 21, defective: 1 }] } }),
    );
  });

  it('a touched counter travels with Repeat too', async () => {
    const posted = mockApi([]);
    render(<PrintersPage />);
    await waitForCard();
    fireEvent.click(await screen.findByTestId('plate-defects-toggle'));
    const lid = (await screen.findByTestId('part-defective-21')) as HTMLInputElement;
    fireEvent.change(lid, { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: /Repeat print/i }));
    await waitFor(() =>
      expect(posted.repeat).toEqual({ defects: { parts: [{ id: 21, defective: 2 }] } }),
    );
  });

  it('an untouched row sends no body at all', async () => {
    const posted = mockApi([]);
    render(<PrintersPage />);
    await waitForCard();
    await screen.findByTestId('plate-defects-toggle');
    fireEvent.click(screen.getByRole('button', { name: /Clear plate/i }));
    await waitFor(() => expect(posted.clearCalls).toBe(1));
    expect(posted.clear).toBeNull();
  });

  it('with a queue the widget draws the only row', async () => {
    mockApi([pendingItem]);
    render(<PrintersPage />);
    await waitForCard();
    await waitFor(() => {
      expect(screen.getAllByTestId('plate-defects-toggle')).toHaveLength(1);
      expect(screen.getAllByRole('button', { name: /Clear plate/i })).toHaveLength(1);
    });
  });
});

describe('the compact card', () => {
  beforeEach(() => localStorage.setItem('printerCardSize', '1'));

  it('offers the defects row beside its pair as well', async () => {
    mockApi([]);
    render(<PrintersPage />);
    await waitForCard();
    expect(await screen.findByTestId('plate-defects-toggle')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Clear plate/i })).toBeInTheDocument();
  });

  it('carries the numbers with the answer', async () => {
    const posted = mockApi([]);
    render(<PrintersPage />);
    await waitForCard();
    fireEvent.click(await screen.findByTestId('plate-defects-toggle'));
    const lid = (await screen.findByTestId('part-defective-21')) as HTMLInputElement;
    fireEvent.change(lid, { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: /Clear plate/i }));
    await waitFor(() =>
      expect(posted.clear).toEqual({ defects: { parts: [{ id: 21, defective: 1 }] } }),
    );
  });
});


/**
 * m173 changed the queue rows, not this prompt.
 *
 * The row held for Clear/Repeat keeps its file (§9), but the prompt is drawn from
 * live printer state and its two answers are about the plate — ruled out of scope
 * for a mark. What must hold is that the defects row and both answers are
 * untouched by the change, and that no queue mark or tooltip leaks in here.
 */
describe('the defects prompt after the queue-source mark landed', () => {
  beforeEach(() => localStorage.setItem('printerCardSize', '2'));

  it('is unchanged, and carries no queue-source mark', async () => {
    const posted = mockApi([{ id: 11, printer_id: 1, status: 'pending', archive_id: 500, position: 1, source_storage: 'ready' }]);
    render(<PrintersPage />);
    await waitForCard();

    // The defects row and the two answers still work exactly as before.
    fireEvent.click(await screen.findByTestId('plate-defects-toggle'));
    const lid = (await screen.findByTestId('part-defective-21')) as HTMLInputElement;
    fireEvent.change(lid, { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: /Clear plate/i }));
    await waitFor(() => expect(posted.clear).toEqual({ defects: { parts: [{ id: 21, defective: 1 }] } }));

    for (const label of ['File saved', 'Saving the file', 'Uses the original', 'Saved copy lost']) {
      expect(screen.queryByLabelText(label)).toBeNull();
    }
  });
});
