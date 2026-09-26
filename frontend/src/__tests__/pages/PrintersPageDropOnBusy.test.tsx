/**
 * A file dropped on a busy or offline printer's card is queued, not refused
 * (upstream 47a37618, #2849).
 *
 * The drop never prints by itself: it uploads the file and opens the Schedule
 * dialog pinned to this printer, which adds a queue item. The card still
 * refused it unless the printer was connected and neither RUNNING nor PAUSE —
 * a red "Printer busy", nothing uploaded, no toast — although loading the queue
 * while a print runs is exactly what the queue is for. The overlay now says
 * which will happen: "Drop to print" when the job would start at once, "Drop to
 * queue" when it would wait (a print running or paused, a plate not yet
 * cleared, the printer offline).
 *
 * The card's Print button is NOT changed: in BamDude it is a direct print
 * (PrintModal reprint), a different path with its own busy rule.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockPrinter = {
  id: 1,
  name: 'Workhorse',
  ip_address: '192.168.1.100',
  serial_number: '01P00A000000001',
  access_code: '12345678',
  model: 'X1C',
  enabled: true,
  nozzle_diameter: 0.4,
  nozzle_type: 'stainless_steel',
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const uploads: string[] = [];

function renderWith(over: Record<string, unknown>) {
  const status = {
    connected: true, state: 'IDLE', progress: 0, layer_num: 0, total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 }, remaining_time: 0, filename: null,
    wifi_signal: -29, speed_level: 2, vt_tray: [], ams: [],
    ...over,
  };
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, status])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.post('/api/v1/library/files', () => {
      uploads.push('upload');
      return HttpResponse.json({ id: 7, filename: 'part.gcode.3mf', metadata: {}, file_tags: [], outcome: 'created' });
    }),
    http.delete('/api/v1/library/files/:id', () => HttpResponse.json({ success: true })),
  );
  render(<PrintersPage />);
}

/** Any element inside the card: drag events bubble to the card's handlers. */
async function card(): Promise<HTMLElement> {
  return screen.findByText('Workhorse');
}

const file = () => new File(['PK'], 'part.gcode.3mf', { type: 'application/octet-stream' });

describe('PrintersPage — a file dropped on a busy printer is queued', () => {
  beforeEach(() => {
    uploads.length = 0;
  });

  it('offers to queue while a print is running', async () => {
    renderWith({ state: 'RUNNING' });
    fireEvent.dragEnter(await card(), { dataTransfer: { files: [file()] } });
    expect(await screen.findByText('Drop to queue')).toBeInTheDocument();
    expect(screen.queryByText('Printer busy')).toBeNull();
    expect(screen.queryByText('Drop to print')).toBeNull();
  });

  it('offers to queue while a print is paused', async () => {
    renderWith({ state: 'PAUSE' });
    fireEvent.dragEnter(await card(), { dataTransfer: { files: [file()] } });
    expect(await screen.findByText('Drop to queue')).toBeInTheDocument();
  });

  it('offers to queue for an offline printer', async () => {
    renderWith({ connected: false });
    fireEvent.dragEnter(await card(), { dataTransfer: { files: [file()] } });
    expect(await screen.findByText('Drop to queue')).toBeInTheDocument();
  });

  it('offers to queue while the plate waits to be cleared', async () => {
    renderWith({ state: 'FINISH', awaiting_plate_clear: true });
    fireEvent.dragEnter(await card(), { dataTransfer: { files: [file()] } });
    expect(await screen.findByText('Drop to queue')).toBeInTheDocument();
  });

  it('still says print when the printer would start it at once', async () => {
    renderWith({ state: 'IDLE' });
    fireEvent.dragEnter(await card(), { dataTransfer: { files: [file()] } });
    expect(await screen.findByText('Drop to print')).toBeInTheDocument();
    expect(screen.queryByText('Drop to queue')).toBeNull();
  });

  it('uploads the dropped file while the printer is running', async () => {
    renderWith({ state: 'RUNNING' });
    const el = await card();
    fireEvent.dragEnter(el, { dataTransfer: { files: [file()] } });
    fireEvent.drop(el, { dataTransfer: { files: [file()] } });
    await waitFor(() => expect(uploads).toHaveLength(1));
  });
});
