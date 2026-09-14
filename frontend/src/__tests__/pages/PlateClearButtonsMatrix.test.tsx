/**
 * The two answers to a full plate — "Repeat print" and "Clear plate" — across
 * the card-size × queue matrix. A finished printer awaiting a clear must offer
 * BOTH actions in every size: the size-S card once showed only the clear icon
 * (queue case) and the pair must not double up with the queue widget's green
 * CTA on expanded cards (reported 2026-08-24).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
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

function mockApi(pending: unknown[]) {
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(finishedAwaitingClear)),
    http.get('/api/v1/queue/', () => HttpResponse.json(pending)),
  );
}

async function waitForCard() {
  await waitFor(() => {
    expect(screen.getByText('X2D Farm')).toBeInTheDocument();
  });
}

describe('size S (compact)', () => {
  beforeEach(() => localStorage.setItem('printerCardSize', '1'));

  it('names the next queued job in its queue strip', async () => {
    mockApi([pendingItem]);
    render(<PrintersPage />);
    await waitForCard();
    await waitFor(() => {
      expect(screen.getByText('Next: next_job.gcode.3mf')).toBeInTheDocument();
    });
  });

  it('offers both actions when a queue is waiting', async () => {
    mockApi([pendingItem]);
    render(<PrintersPage />);
    await waitForCard();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Clear plate/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Repeat print/i })).toBeInTheDocument();
    });
  });

  it('offers both actions with no queue at all', async () => {
    mockApi([]);
    render(<PrintersPage />);
    await waitForCard();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Clear plate/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Repeat print/i })).toBeInTheDocument();
    });
  });
});

describe('expanded', () => {
  beforeEach(() => localStorage.setItem('printerCardSize', '2'));

  it('with a queue the green widget pair is the only one drawn', async () => {
    mockApi([pendingItem]);
    render(<PrintersPage />);
    await waitForCard();
    await waitFor(() => {
      // widget CTA present…
      expect(screen.getAllByRole('button', { name: /Repeat print/ })).toHaveLength(1);
      expect(screen.getAllByRole('button', { name: /Clear plate/i })).toHaveLength(1);
    });
  });

  it('with no queue the standalone yellow pair is drawn', async () => {
    mockApi([]);
    render(<PrintersPage />);
    await waitForCard();
    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /Repeat print/ })).toHaveLength(1);
      expect(screen.getAllByRole('button', { name: /Clear plate/i })).toHaveLength(1);
    });
  });
});


/**
 * The plate-clear surface after m173 — deliberately UNMARKED.
 *
 * A completed row held for Clear/Repeat does keep its file (§9's table), but this
 * prompt is drawn from live printer state, not from the queue row, and the two
 * answers it offers are about the PLATE. Ruled out of scope: what has to hold is
 * the negative — the queue's own mark does not leak onto this surface, and the
 * pair of answers is still exactly one pair.
 */
describe('the plate prompt carries no queue-source mark', () => {
  beforeEach(() => localStorage.setItem('printerCardSize', '2'));

  it('draws neither mark nor tooltip, and still offers both answers once', async () => {
    // The waiting row reports a stored copy; the prompt must not repeat it.
    mockApi([{ ...pendingItem, source_storage: 'ready' }]);
    render(<PrintersPage />);
    await waitForCard();

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /Repeat print/ })).toHaveLength(1);
      expect(screen.getAllByRole('button', { name: /Clear plate/i })).toHaveLength(1);
    });
    for (const label of ['File saved', 'Saving the file', 'Uses the original', 'Saved copy lost']) {
      expect(screen.queryByLabelText(label)).toBeNull();
    }
    expect(screen.queryByTitle(/stored copy|stored file|File saved for the queue/)).toBeNull();
  });
});
